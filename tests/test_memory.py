import hashlib
import json
import math
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from indeces.memory import DECAY
from tests.test_memory_selection_performance import _CachedLegacyMemoryGraph as MemoryGraph
# These fixtures preserve the historical v1 eager/shadow audit contract.
# Production static-v2 behavior is tested in test_subgraph_byte_identity and
# test_neighborhood_audit, including frozen legacy values and no shadow writes.
from indeces.run_records import digest, validate_graph_audit


class MemoryGraphTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.graph = MemoryGraph(self.db, self_marks=("Indeces", "botname"))

    def tearDown(self):
        self.db.close()

    def fact(self, source, marks, text=None, scope="guild/channel"):
        text = text or f"A sourced statement about {', '.join(marks)}."
        return self.graph.add(scope, source, "author-17", [
            {"text": text, "quote": f"original quotation from {source}", "marks": marks}
        ], 1.0)[0]

    def weight(self, a, b, kind="dynamic", scope="guild/channel"):
        return self.db.execute(
            f"SELECT weight FROM memory_{kind} WHERE scope=? AND a=? AND b=?",
            (scope, *sorted((a, b)))).fetchone()[0]

    def test_annotation_has_no_dynamic_effect_and_add_is_deduplicated(self):
        first = self.fact("discord-1", ["alpha", "beta"])
        second = self.fact("discord-1", ["alpha", "beta"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 1)
        for table in ("memory_dynamic", "memory_events", "memory_cycles"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_positive_npmi_retains_source_evidence_and_negative_edge_is_omitted(self):
        ab1 = self.fact("ab1", ["alpha", "beta"])
        ab2 = self.fact("ab2", ["alpha", "beta"])
        self.fact("ac", ["alpha", "gamma"])
        self.fact("c", ["gamma"])
        row = self.db.execute(
            "SELECT weight,evidence_json,co_count FROM memory_static "
            "WHERE a='alpha' AND b='beta'").fetchone()
        self.assertAlmostEqual(row[0], 0.4150, places=4)
        self.assertEqual(json.loads(row[1]), [ab1["id"], ab2["id"]])
        self.assertEqual(row[2], 2)
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM memory_static WHERE a='alpha' AND b='gamma'").fetchone())

    def test_scope_isolation_includes_queries_and_graph_edges(self):
        self.fact("source-a", ["alpha", "beta"], scope="scope-a")
        self.fact("source-b", ["gamma", "delta"], scope="scope-b")
        self.assertEqual(self.graph.retrieve("scope-b", ["alpha"], "alpha beta", 2), [])
        results = self.graph.retrieve("scope-a", ["alpha"], "alpha beta", 2)
        self.assertEqual([r["source_id"] for r in results], ["source-a"])
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM memory_events WHERE scope='scope-b'").fetchone())

    def test_case_boundaries_and_own_aliases_do_not_create_artificial_hits(self):
        record = self.fact("named", ["bot", "INDECES", "BOTNAME"])
        self.assertEqual(record["marks"], ["bot"])
        for query in ("robot", "botanical", "bot2", "_bot", "Indeces botname"):
            self.assertEqual(self.graph.retrieve("guild/channel", ["bot"], query, 2), [])
        results = self.graph.retrieve("guild/channel", ["BOT"], "BOT!", 3)
        self.assertEqual(results[0]["direct_marks"], ["bot"])

    def test_context_gate_blocks_ambiguous_one_hop(self):
        self.fact("context", ["alpha", "beta", "gamma"])
        self.fact("beta-only", ["beta"])
        self.fact("unrelated", ["unrelated"])
        no_context = self.graph.retrieve("guild/channel", ["alpha"], "alpha", 2)
        self.assertEqual([r["source_id"] for r in no_context], ["context"])
        self.assertEqual(no_context[0]["expanded_marks"], [])
        with_context = self.graph.retrieve("guild/channel", ["alpha", "gamma"], "alpha gamma", 3)
        self.assertIn("beta-only", [r["source_id"] for r in with_context])
        self.assertTrue(any("beta" in r["expanded_marks"] for r in with_context))

    def test_expansion_is_exactly_one_hop(self):
        self.fact("ab", ["alpha", "beta"])
        self.fact("bc", ["beta", "gamma"])
        self.fact("other", ["other"])
        results = self.graph.retrieve("guild/channel", ["alpha"], "alpha", 2)
        extras = {m for r in results for m in r["expanded_marks"]}
        self.assertIn("beta", extras)
        self.assertNotIn("gamma", extras)
        self.assertTrue(all(e["from_mark"] == "alpha" for r in results for e in r["evidence"]))

    def test_live_dynamic_weights_change_ranking_without_rewriting_static(self):
        self.fact("ab", ["alpha", "beta"])
        self.fact("ac", ["alpha", "gamma"])
        self.fact("other", ["other"])
        static_before = list(self.db.execute("SELECT * FROM memory_static ORDER BY a,b"))
        before = self.graph.retrieve("guild/channel", ["alpha"], "alpha", 2, event_id="first", ranking_mode="dynamic")
        self.assertEqual(before[0]["source_id"], "ac")  # equal weights: newest source first
        self.graph.retrieve("guild/channel", ["alpha", "beta"], "alpha beta", 3, event_id="cofire", ranking_mode="dynamic")
        after = self.graph.retrieve("guild/channel", ["alpha"], "alpha", 4, event_id="next", ranking_mode="dynamic")
        self.assertEqual(after[0]["source_id"], "ab")
        self.assertGreater(after[0]["dynamic_score"], after[1]["dynamic_score"])
        self.assertEqual(static_before, list(self.db.execute("SELECT * FROM memory_static ORDER BY a,b")))

    def test_default_static_ranking_is_unchanged_by_shadow_learning(self):
        self.fact("ab", ["alpha", "beta"])
        self.fact("ac", ["alpha", "gamma"])
        self.fact("other", ["other"])
        before = self.graph.retrieve("guild/channel", [], "alpha", 2, event_id="static-first")
        self.graph.retrieve("guild/channel", [], "alpha beta", 3, event_id="shadow-cofire")
        audit = {}
        after = self.graph.retrieve("guild/channel", [], "alpha", 4, event_id="static-next", audit=audit)
        self.assertEqual([r["source_id"] for r in before], ["ac", "ab"])
        self.assertEqual([r["source_id"] for r in after], ["ac", "ab"])
        self.assertEqual([r["ranking_score"] for r in before], [r["ranking_score"] for r in after])
        self.assertGreater(self.weight("alpha", "beta"), self.weight("alpha", "gamma"))
        selection = audit["selection"]
        self.assertEqual((selection["ranking_mode"], selection["weight_basis"]), ("static", "static_npmi"))
        self.assertTrue(all(e["effective_score"] == e["static_score"] for e in selection["edge_statistics"]))
        validate_graph_audit(audit)

    def test_shadow_supported_pair_without_positive_npmi_cannot_expand_static(self):
        self.fact("ab", ["alpha", "beta"])
        self.fact("a", ["alpha"])
        self.fact("b", ["beta"])
        self.assertIsNone(self.db.execute("SELECT weight FROM memory_static WHERE a='alpha' AND b='beta'").fetchone())
        reinforced = {}
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="shadow-pair", audit=reinforced)
        self.assertGreater(self.weight("alpha", "beta"), 0)
        self.assertEqual(reinforced["selection"]["edge_statistics"], [])
        validate_graph_audit(reinforced)
        audit = {}
        selected = self.graph.retrieve("guild/channel", [], "alpha", 3, event_id="static-only", audit=audit)
        self.assertNotIn("b", [r["source_id"] for r in selected])
        self.assertTrue(all(not r["expanded_marks"] for r in selected))
        self.assertEqual(audit["selection"]["edge_statistics"], [])
        validate_graph_audit(audit)

    def test_invalid_ranking_mode_fails_before_graph_changes(self):
        self.fact("ab", ["alpha", "beta"])
        with self.assertRaisesRegex(ValueError, "ranking_mode"):
            self.graph.retrieve("guild/channel", [], "alpha beta", 2, ranking_mode="unknown")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0], 0)

    def test_legacy_dynamic_audit_without_mode_remains_verifiable(self):
        self.fact("ab", ["alpha", "beta"])
        audit = {}
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="legacy-contract",
                            ranking_mode="dynamic", audit=audit)
        del audit["selection"]["ranking_mode"]
        del audit["selection"]["weight_basis"]
        audit["durable_payload_sha256"] = digest({k: v for k, v in audit.items() if k != "durable_payload_sha256"})
        validate_graph_audit(audit)

    def test_explicit_mode_and_basis_must_agree(self):
        self.fact("ab", ["alpha", "beta"])
        audit = {}
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="mode-contract", audit=audit)
        audit["selection"]["weight_basis"] = "dynamic_or_static"
        audit["durable_payload_sha256"] = digest({k: v for k, v in audit.items() if k != "durable_payload_sha256"})
        with self.assertRaisesRegex(ValueError, "weight basis"):
            validate_graph_audit(audit)

    def test_replay_cannot_change_ranking_mode_or_rewrite_stored_evidence(self):
        self.fact("ab", ["alpha", "beta"])
        tables = ("memory_static", "memory_support", "memory_dynamic", "memory_events",
                  "memory_cycles", "memory_scopes", "memory_event_audits")
        for original_mode, requested_mode in (("dynamic", "static"), ("static", "dynamic")):
            event_id = "mode-replay-" + original_mode
            self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id=event_id,
                                ranking_mode=original_mode)
            before = {table: list(self.db.execute("SELECT * FROM " + table)) for table in tables}
            audit = {"untouched": True}
            with self.assertRaisesRegex(ValueError, "different ranking mode"):
                self.graph.retrieve("guild/channel", [], "alpha beta", 3, event_id=event_id,
                                    ranking_mode=requested_mode, audit=audit)
            self.assertEqual(audit, {"untouched": True})
            self.assertEqual({table: list(self.db.execute("SELECT * FROM " + table)) for table in tables}, before)
            self.assertFalse(self.db.in_transaction)

    def test_original_dynamic_audit_without_mode_cannot_be_replayed_as_static(self):
        self.fact("ab", ["alpha", "beta"])
        audit = {}
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="legacy-mode-source",
                            ranking_mode="dynamic", audit=audit)
        original = json.loads(self.stored_audit("legacy-mode-source"))
        del original["selection"]["ranking_mode"]
        del original["selection"]["weight_basis"]
        original["event_id"] = "legacy-without-mode"
        # A synthetic immutable old receipt; no production migration writes.
        serialized = json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.db:
            self.db.execute("INSERT INTO memory_event_audits VALUES(?,?,?)",
                            ("guild/channel", original["event_id"], serialized))
        before_changes = self.db.total_changes
        with self.assertRaisesRegex(ValueError, "different ranking mode"):
            self.graph.retrieve("guild/channel", [], "alpha beta", 3, event_id=original["event_id"])
        self.assertEqual(self.stored_audit(original["event_id"]), serialized)
        self.assertEqual(self.db.total_changes, before_changes)

    def test_event_retry_is_idempotent_and_each_new_event_decays_once(self):
        self.fact("ab", ["alpha", "beta"])
        self.graph.retrieve("guild/channel", ["alpha", "beta"], "alpha beta", 2, event_id="discord-2")
        self.assertEqual(self.weight("alpha", "beta"), 1 * DECAY + 1)
        self.graph.retrieve("guild/channel", ["alpha", "beta"], "alpha beta", 99, event_id="discord-2")
        self.assertEqual(self.weight("alpha", "beta"), 1.99)
        self.graph.retrieve("guild/channel", ["alpha"], "alpha", 3, event_id="discord-3")
        self.assertEqual(self.weight("alpha", "beta"), round(1.99 * DECAY, 6))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memory_cycles").fetchone()[0], 2)

    def test_event_id_input_collision_is_reported_and_rolled_back(self):
        self.fact("ab", ["alpha", "beta"])
        self.graph.retrieve("guild/channel", ["alpha"], "alpha", 2, event_id="same")
        weight = self.weight("alpha", "beta")
        with self.assertRaisesRegex(ValueError, "different input"):
            self.graph.retrieve("guild/channel", ["beta"], "beta", 3, event_id="same")
        self.assertEqual(self.weight("alpha", "beta"), weight)

    def test_duplicate_labels_do_not_reinforce_self_pairs_and_direct_hits_are_capped(self):
        self.fact("single", ["alpha", "ALPHA", "alpha"])
        self.graph.retrieve("guild/channel", ["alpha", "alpha"], "alpha", 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memory_dynamic").fetchone()[0], 0)
        self.fact("many", ["alpha", "bravo", "charlie", "delta", "echo"])
        self.graph.retrieve("guild/channel", [], "alpha bravo charlie delta echo", 3)
        last = json.loads(self.db.execute(
            "SELECT marks_json FROM memory_events ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(len(last), 4)
        self.assertEqual(len(last), len(set(last)))

    def test_strongest_expansion_provenance_wins_independent_of_direct_hit_order(self):
        self.fact("lt", ["long", "target"])
        self.fact("st", ["short", "target"])
        for index in range(3):
            self.fact(f"long-{index}", ["long"])
        for index in range(5):
            self.fact(f"other-{index}", [f"other{index}"])
        results = self.graph.retrieve("guild/channel", ["long", "short"], "long short", 2)
        expansion_edges = [e for r in results for e in r["evidence"] if e["mark"] == "target"]
        self.assertTrue(expansion_edges)
        self.assertTrue(all(e["from_mark"] == "short" for e in expansion_edges))

    def test_source_provenance_and_reference_character_limits(self):
        for index in range(5):
            self.fact(f"discord-{index}", ["alpha"], text="知" * 300)
        results = self.graph.retrieve("guild/channel", ["alpha"], "alpha", 2)
        self.assertEqual(len(results), 3)
        self.assertLessEqual(sum(len(r["text"]) for r in results), 400)
        self.assertTrue(all(r["text_truncated"] for r in results))
        for result in results:
            self.assertEqual(result["author_id"], "author-17")
            self.assertEqual(result["quote"], f"original quotation from {result['source_id']}")
            self.assertIsInstance(result["id"], int)
            self.assertIn("retrieval_event_id", result)

    def test_existing_dynamic_weights_are_not_reseeded_on_static_rebuild(self):
        self.fact("ab", ["alpha", "beta"])
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="seed")
        before = self.weight("alpha", "beta")
        self.fact("ac", ["alpha", "gamma"])
        self.assertEqual(self.weight("alpha", "beta"), before)
        self.assertEqual(self.db.execute(
            "SELECT seed_weight FROM memory_dynamic WHERE a='alpha' AND b='beta'").fetchone()[0], 1.0)

    def test_replacement_archives_old_version_and_retains_audit_rows(self):
        old = self.fact("kb:document:v1", ["alpha", "obsolete"])
        self.graph.retrieve("guild/channel", [], "alpha obsolete", 2, event_id="old-use")
        old_weight = self.weight("alpha", "obsolete")
        self.assertEqual(self.graph.deactivate_source("guild/channel", "kb:document:v1"), 1)
        self.assertEqual(self.graph.deactivate_source("guild/channel", "kb:document:v1"), 0)
        new = self.fact("kb:document:v2", ["alpha", "fresh"])
        stored = self.db.execute(
            "SELECT active,archived_at,quote FROM memory_records WHERE id=?", (old["id"],)).fetchone()
        self.assertEqual(stored[0], 0)
        self.assertIsNotNone(stored[1])
        self.assertEqual(stored[2], old["quote"])
        self.assertEqual(self.weight("alpha", "obsolete"), old_weight)
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM memory_static WHERE a='alpha' AND b='obsolete'").fetchone())
        results = self.graph.retrieve("guild/channel", [], "alpha fresh", 3, event_id="new-use")
        self.assertEqual([r["source_id"] for r in results], ["kb:document:v2"])
        self.assertTrue(all(old["id"] not in e["source_record_ids"]
                            for r in results for e in r["evidence"]))
        self.assertIn(new["id"], results[0]["evidence"][0]["source_record_ids"])
        # An old background job retry cannot reactivate the archived source.
        retried = self.fact("kb:document:v1", ["alpha", "obsolete"])
        self.assertFalse(retried["active"])
        self.assertEqual(retried["id"], old["id"])

    def test_stale_dynamic_edge_cannot_expand_between_separately_active_marks(self):
        old = self.fact("kb:old:v1", ["alpha", "beta"])
        self.fact("kb:a:v1", ["alpha"])
        self.fact("kb:b:v1", ["beta"])
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="prior-cofire")
        self.assertGreater(self.weight("alpha", "beta"), 0)
        self.graph.deactivate_source("guild/channel", "kb:old:v1")
        results = self.graph.retrieve("guild/channel", [], "alpha", 3, event_id="after-retire")
        self.assertEqual([r["source_id"] for r in results], ["kb:a:v1"])
        self.assertEqual(results[0]["expanded_marks"], [])
        self.assertEqual(results[0]["evidence"], [])
        # The archived edge still exists for audit but cannot rank facts.
        self.assertGreater(self.weight("alpha", "beta"), 0)
        self.assertEqual(self.db.execute(
            "SELECT active FROM memory_records WHERE id=?", (old["id"],)).fetchone()[0], 0)

    def test_retirement_is_scope_isolated(self):
        self.fact("kb:same:v1", ["alpha"], scope="scope-a")
        self.fact("kb:same:v1", ["alpha"], scope="scope-b")
        self.graph.deactivate_source("scope-a", "kb:same:v1")
        self.assertEqual(self.graph.retrieve("scope-a", [], "alpha", 2), [])
        self.assertEqual(len(self.graph.retrieve("scope-b", [], "alpha", 2)), 1)

    def stored_audit(self, event_id, *, scope="guild/channel"):
        return self.db.execute("SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
                               (scope, event_id)).fetchone()[0]

    def test_audit_reconstructs_static_seed_decay_and_direct_reinforcement(self):
        record = self.fact("source", ["alpha", "beta"])
        audit = {}
        result = self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="audited", audit=audit)
        observation = audit["observation"]
        self.assertTrue(observation["applied"])
        self.assertFalse(observation["seeded_before"])
        self.assertTrue(observation["seeded_after"])
        transition = observation["changed_edges"][0]
        self.assertEqual((transition["a"], transition["b"]), ("alpha", "beta"))
        self.assertIsNone(transition["before_weight"])
        self.assertEqual(transition["created_by"], "static_seed")
        self.assertEqual(transition["after_seed"], 1.0)
        self.assertEqual(transition["after_decay"], round(transition["after_seed"] * observation["parameters"]["decay"], 6))
        self.assertEqual(transition["reinforcement_before"], transition["after_decay"])
        self.assertEqual(transition["reinforcement_added"], observation["parameters"]["eta"])
        self.assertEqual(transition["after_weight"], round(transition["after_decay"] + transition["reinforcement_added"], 6))
        self.assertEqual(transition["after_weight"], self.weight("alpha", "beta"))
        self.assertTrue(transition["active_source_support"])
        self.assertEqual(transition["source_record_ids"], [record["id"]])
        self.assertEqual(audit["selection"]["selected_record_ids"], [r["id"] for r in result])
        serialized = self.stored_audit("audited")
        self.assertEqual(audit["durable_payload_sha256"], hashlib.sha256(serialized.encode()).hexdigest())
        self.assertEqual(json.loads(serialized)["observation"], observation)

    def test_audit_reconstructs_npmi_from_active_global_counts(self):
        self.fact("ab1", ["alpha", "beta"])
        self.fact("ab2", ["alpha", "beta"])
        self.fact("ac", ["alpha", "gamma"])
        self.fact("c", ["gamma"])
        audit = {}
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="statistics", audit=audit)
        selection = audit["selection"]
        self.assertTrue(selection["static_formula_reproducible"])
        self.assertEqual(selection["active_record_count"], 4)
        self.assertEqual(selection["mark_frequencies"], {"alpha": 3, "beta": 2, "gamma": 2})
        for edge in selection["edge_statistics"]:
            n = selection["active_record_count"]
            p_ab = edge["co_count"] / n
            p_a, p_b = (selection["mark_frequencies"][mark] / n for mark in (edge["a"], edge["b"]))
            npmi = 1.0 if p_ab == 1 else math.log(p_ab / (p_a * p_b)) / -math.log(p_ab)
            self.assertEqual(edge["static_score"], round(npmi, 4) if npmi > 0 else 0.0)
            self.assertEqual(edge["co_count"], len(edge["source_record_ids"]))
        live_edges = {(edge["a"], edge["b"]): edge for edge in selection["edge_statistics"]}
        for candidate in selection["ranked_candidates"]:
            for evidence in candidate["evidence"]:
                pair = tuple(sorted((evidence["from_mark"], evidence["mark"])))
                edge = live_edges[pair]
                for field in ("co_count", "source_record_ids", "context", "dynamic_last_event_id",
                              "static_score", "dynamic_score", "effective_score"):
                    self.assertEqual(evidence[field], edge[field])

    def test_audit_captures_decay_of_inactive_unsupported_dynamic_history(self):
        self.fact("old", ["alpha", "beta"])
        self.fact("current", ["alpha"])
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="old-learning")
        before = self.weight("alpha", "beta")
        self.graph.deactivate_source("guild/channel", "old")
        audit = {}
        self.graph.retrieve("guild/channel", [], "alpha", 3, event_id="inactive-decay", audit=audit)
        transition = audit["observation"]["changed_edges"][0]
        self.assertFalse(transition["active_source_support"])
        self.assertEqual(transition["source_record_ids"], [])
        self.assertEqual(transition["before_weight"], before)
        self.assertEqual(transition["after_decay"], round(before * DECAY, 6))
        self.assertEqual(transition["reinforcement_added"], 0)
        self.assertEqual(transition["after_weight"], transition["after_decay"])
        self.assertEqual(audit["selection"]["expanded_marks"], [])

    def test_audit_captures_new_direct_pair_without_source_support_or_seed(self):
        self.fact("a", ["alpha"])
        self.fact("b", ["beta"])
        audit = {}
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="unsupported-pair", audit=audit)
        transition = audit["observation"]["changed_edges"][0]
        self.assertEqual(transition["created_by"], "direct_reinforcement")
        self.assertIsNone(transition["before_weight"])
        self.assertIsNone(transition["after_seed"])
        self.assertFalse(transition["decay_applied"])
        self.assertIsNone(transition["after_decay"])
        self.assertEqual(transition["reinforcement_before"], 0)
        self.assertEqual(transition["after_weight"], 1)
        self.assertFalse(transition["active_source_support"])
        self.assertEqual(audit["selection"]["live_edge_count"], 0)

    def test_no_hit_audit_is_durable_without_seeding_events_or_decay(self):
        self.fact("source", ["alpha", "beta"])
        audit = {}
        self.assertEqual(self.graph.retrieve("guild/channel", [], "unrelated", 2, event_id="nohit", audit=audit), [])
        self.assertEqual(audit["observation"]["status"], "no_hit")
        self.assertFalse(audit["observation"]["applied"])
        self.assertEqual(audit["observation"]["changed_edges"], [])
        serialized = self.stored_audit("nohit")
        for table in ("memory_dynamic", "memory_events", "memory_cycles", "memory_scopes"):
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        self.graph.retrieve("guild/channel", [], "unrelated", 99, event_id="nohit", audit=audit)
        self.assertTrue(audit["replay"])
        self.assertEqual(self.stored_audit("nohit"), serialized)
        with self.assertRaisesRegex(ValueError, "different input"):
            self.graph.retrieve("guild/channel", [], "different", 99, event_id="nohit")

    def test_replay_keeps_original_audit_but_selects_current_active_sources(self):
        old = self.fact("old", ["alpha", "beta"])
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="replayed")
        original = self.stored_audit("replayed")
        weight = self.weight("alpha", "beta")
        self.graph.deactivate_source("guild/channel", "old")
        new = self.fact("new", ["alpha", "beta"])
        audit = {}
        results = self.graph.retrieve("guild/channel", [], "alpha beta", 99, event_id="replayed", audit=audit)
        self.assertTrue(audit["replay"])
        self.assertEqual(audit["observation"]["status"], "replay")
        self.assertFalse(audit["observation"]["applied"])
        self.assertEqual(audit["observation"]["changed_edges"], [])
        self.assertEqual(self.weight("alpha", "beta"), weight)
        self.assertEqual([r["id"] for r in results], [new["id"]])
        self.assertEqual(json.loads(original)["selection"]["selected_record_ids"], [old["id"]])
        self.assertEqual(self.stored_audit("replayed"), original)
        self.assertEqual(audit["original_event"]["payload_sha256"], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(audit["original_event"]["ranking_mode"], "static")
        validate_graph_audit(audit)
        audit["original_event"]["ranking_mode"] = "dynamic"
        with self.assertRaisesRegex(ValueError, "replayed graph ranking mode"):
            validate_graph_audit(audit)
        for statement in ("UPDATE memory_event_audits SET payload_json='{}'", "DELETE FROM memory_event_audits"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                with self.db:
                    self.db.execute(statement)

    def test_audit_insert_failure_rolls_back_learning_selection_and_event(self):
        self.fact("source", ["alpha", "beta"])
        self.db.executescript("""
            CREATE TRIGGER reject_audit BEFORE INSERT ON memory_event_audits
            BEGIN SELECT RAISE(ABORT, 'synthetic audit write failure'); END;
        """)
        audit = {"untouched": True}
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic audit write failure"):
            self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="failed-audit", audit=audit)
        self.assertEqual(audit, {"untouched": True})
        for table in ("memory_dynamic", "memory_events", "memory_cycles", "memory_scopes", "memory_event_audits"):
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        self.assertEqual(self.weight("alpha", "beta", kind="static"), 1.0)

    def test_selection_failure_rolls_back_learning_before_audit_publication(self):
        self.fact("source", ["alpha", "beta"])
        with patch.object(self.graph, "_selection", side_effect=RuntimeError("synthetic selection failure")):
            with self.assertRaisesRegex(RuntimeError, "selection failure"):
                self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="selection-failure")
        for table in ("memory_dynamic", "memory_events", "memory_cycles", "memory_scopes", "memory_event_audits"):
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_selection_audit_explains_rank_caps_and_source_hashes(self):
        for index in range(5):
            self.fact("source-" + str(index), ["alpha"], text="知" * 300)
        audit = {}
        results = self.graph.retrieve("guild/channel", [], "alpha", 2, event_id="selection", audit=audit)
        selection = audit["selection"]
        self.assertEqual(len(selection["ranked_candidates"]), 5)
        self.assertEqual(selection["selected_record_ids"], [r["id"] for r in results])
        self.assertEqual(selection["used_text_characters"], sum(len(r["text"]) for r in results))
        self.assertEqual(selection["limits"]["references"], 3)
        self.assertEqual([c["record_id"] for c in selection["ranked_candidates"]], [5, 4, 3, 2, 1])
        for selected, result in zip(selection["selected"], results):
            self.assertTrue(selected["text_truncated"])
            self.assertEqual(selected["preview_sha256"], hashlib.sha256(result["text"].encode()).hexdigest())
        self.assertTrue(all(c["text_sha256"] == hashlib.sha256(("知" * 300).encode()).hexdigest()
                            for c in selection["ranked_candidates"]))

    def test_legacy_event_replay_does_not_invent_original_transitions(self):
        self.fact("source", ["alpha", "beta"])
        with self.db:
            self.db.execute("INSERT INTO memory_scopes VALUES(?,1)", ("guild/channel",))
            self.db.execute("INSERT INTO memory_dynamic VALUES(?,?,?,?,?,?,?)",
                            ("guild/channel", "alpha", "beta", 7.0, "[]", 1.0, "legacy"))
            self.db.execute("INSERT INTO memory_events VALUES(?,?,?,?,?)",
                            ("guild/channel", "legacy", 1.0, "alpha beta", '["alpha","beta"]'))
        audit = {}
        before_changes = self.db.total_changes
        with self.assertRaisesRegex(ValueError, "different ranking mode"):
            self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="legacy")
        self.assertEqual(self.db.total_changes, before_changes)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memory_event_audits").fetchone()[0], 0)
        self.graph.retrieve("guild/channel", [], "alpha beta", 2, event_id="legacy", audit=audit,
                            ranking_mode="dynamic")
        self.assertEqual(audit["observation"]["status"], "legacy_replay")
        self.assertFalse(audit["observation"]["original_changes_known"])
        self.assertFalse(audit["observation"]["applied"])
        self.assertEqual(audit["observation"]["changed_edges"], [])
        self.assertEqual(self.weight("alpha", "beta"), 7)

    def test_audit_and_retry_survive_database_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                graph = MemoryGraph(db)
                graph.add("scope", "source", "author", [{"text": "alpha beta", "quote": "alpha beta", "marks": ["alpha", "beta"]}], 1.0)
                graph.retrieve("scope", [], "alpha beta", 2.0, event_id="durable")
                original = db.execute("SELECT payload_json FROM memory_event_audits").fetchone()[0]
            with closing(sqlite3.connect(path)) as db:
                graph = MemoryGraph(db)
                audit = {}
                graph.retrieve("scope", [], "alpha beta", 3.0, event_id="durable", audit=audit)
                self.assertTrue(audit["replay"])
                self.assertEqual(db.execute("SELECT payload_json FROM memory_event_audits").fetchone()[0], original)
                self.assertEqual(db.execute("SELECT weight FROM memory_dynamic").fetchone()[0], 1.99)


if __name__ == "__main__":
    unittest.main()
