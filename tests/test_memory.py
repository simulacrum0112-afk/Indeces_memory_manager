import json
import sqlite3
import unittest

from indeces.memory import DECAY, MemoryGraph


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
        before = self.graph.retrieve("guild/channel", ["alpha"], "alpha", 2, event_id="first")
        self.assertEqual(before[0]["source_id"], "ac")  # equal weights: newest source first
        self.graph.retrieve("guild/channel", ["alpha", "beta"], "alpha beta", 3, event_id="cofire")
        after = self.graph.retrieve("guild/channel", ["alpha"], "alpha", 4, event_id="next")
        self.assertEqual(after[0]["source_id"], "ab")
        self.assertGreater(after[0]["dynamic_score"], after[1]["dynamic_score"])
        self.assertEqual(static_before, list(self.db.execute("SELECT * FROM memory_static ORDER BY a,b")))

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


if __name__ == "__main__":
    unittest.main()
