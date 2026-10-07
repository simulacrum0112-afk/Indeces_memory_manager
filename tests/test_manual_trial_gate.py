"""Finite-pilot safety tests: synthetic requests and temporary ledgers only."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import asyncio
from contextlib import ExitStack, redirect_stdout
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = (PROJECT_ROOT / "indeces" / "manual_trial"
             if (PROJECT_ROOT / "indeces" / "manual_trial" / "pilot_gate.py").is_file() else PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT if TOOL_ROOT != PROJECT_ROOT else PROJECT_ROOT / "checkpoint-repo"))
if TOOL_ROOT != PROJECT_ROOT:
    from indeces.manual_trial import pilot_gate, pilot_launcher
else:
    import pilot_gate, pilot_launcher
COUNT_ENDPOINT, GENERATION_ENDPOINT, LIVE_ENABLED, MODEL = (pilot_gate.COUNT_ENDPOINT,
    pilot_gate.GENERATION_ENDPOINT, pilot_gate.LIVE_ENABLED, pilot_gate.MODEL)
PilotBlocked, PilotGate, PilotLimits, Tariff = (pilot_gate.PilotBlocked, pilot_gate.PilotGate,
    pilot_gate.PilotLimits, pilot_gate.Tariff)
canonical_hash, synthetic_tariff = pilot_gate.canonical_hash, pilot_gate.synthetic_tariff
DEFAULT_CANDIDATE, GatedMockSession, ReplyOnlyAdapter = (pilot_launcher.DEFAULT_CANDIDATE,
    pilot_launcher.GatedMockSession, pilot_launcher.ReplyOnlyAdapter)
_REPLY_CONTEXT, main, run_mock, synthetic_inputs = (pilot_launcher._REPLY_CONTEXT,
    pilot_launcher.main, pilot_launcher.run_mock, pilot_launcher.synthetic_inputs)


EVIDENCE = {"real_http_requests": 0, "direct_gate_admissions": 0,
            "network_attempts_blocked": 0, "launcher_mock_count_requests": 0,
            "launcher_mock_generation_requests": 0, "direct_post_mock_count_requests": 0,
            "direct_post_mock_generation_requests": 0, "launcher": None}
PAYLOAD_HASH = hashlib.sha256(b"synthetic payload identity, no prompt body").hexdigest()
BUNDLE_HASH = hashlib.sha256(b"synthetic two-item bundle").hexdigest()
CANDIDATE_HASH = hashlib.sha256(b"synthetic candidate identity").hexdigest()


def deny_network(*args, **kwargs):
    EVIDENCE["network_attempts_blocked"] += 1
    raise AssertionError("External network is disabled for pilot safety tests")


def fixture_tariff(**overrides):
    values = {"model": MODEL, "generation_input_per_million": "2",
              "generation_output_per_million": "8", "generation_flat": "0",
              "count_input_per_million": "0", "count_flat": "0.001",
              "count_billing_basis": "verified_flat_fee",
              "provenance": {"kind": "synthetic", "source": "synthetic_fee_fixture", "verified": True}}
    values.update(overrides)
    return Tariff(**values)


class PilotFixture:
    def setup_pilot_fixture(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.counter = 0
        self.gates = []
        self.guards = [patch("socket.create_connection", deny_network),
                       patch.object(socket.socket, "connect", deny_network),
                       patch.object(socket.socket, "connect_ex", deny_network)]
        for guard in self.guards:
            guard.start()

    def teardown_pilot_fixture(self):
        # unittest calls tearDown before registered cleanups. Close Windows
        # ledger handles before deleting this test's temporary directory.
        for gate in self.gates:
            gate.close()
        for guard in reversed(self.guards):
            guard.stop()
        self.directory.cleanup()

    def gate(self, *, tariff=None, limits=None, path=None, **kwargs):
        self.counter += 1
        path = path or self.root / f"synthetic-ledger-{self.counter}.json"
        gate = PilotGate(path, tariff=tariff or fixture_tariff(), limits=limits or PilotLimits(),
                         input_bundle_sha256=BUNDLE_HASH, candidate_identity=CANDIDATE_HASH, **kwargs)
        self.gates.append(gate)
        self.addCleanup(gate.close)
        return gate

    @staticmethod
    def admit(gate, item="item-1", endpoint=COUNT_ENDPOINT, *, stage="reply"):
        token = gate.admit(item, stage, endpoint, PAYLOAD_HASH)
        EVIDENCE["direct_gate_admissions"] += 1
        return token

    @staticmethod
    def settle(gate, token, *, inp=40, out=0, status="completed", billing_override=None):
        row = next(row for row in gate.ledger["requests"] if row["reservation_id"] == token)
        cost = gate.tariff.cost(row["kind"], inp, out)
        billing = {"verified": True, "status": "known", "source": "synthetic_provider_receipt",
                   "amount_usd": format(cost, "f")}
        if billing_override:
            billing.update(billing_override)
        gate.settle(token, status=status, usage={"input_tokens": inp, "output_tokens": out}, billing=billing)
        return cost

    def complete_item(self, gate, item):
        count = self.admit(gate, item, COUNT_ENDPOINT)
        self.settle(gate, count)
        generation = self.admit(gate, item, GENERATION_ENDPOINT)
        self.settle(gate, generation, out=9)


class PilotGateTests(PilotFixture, unittest.TestCase):
    def setUp(self):
        self.setup_pilot_fixture()

    def tearDown(self):
        self.teardown_pilot_fixture()

    def test_defaults_disable_live_and_missing_fee_metadata_never_creates_ledger(self):
        self.assertFalse(LIVE_ENABLED)
        gate = self.gate()
        self.assertEqual(gate.mode, "mock")
        self.assertEqual(gate.ledger["requests"], [])
        path = self.root / "missing-fees.json"
        with self.assertRaises(PilotBlocked) as caught:
            PilotGate(path)
        self.assertEqual(caught.exception.code, "unverified_tariff")
        self.assertFalse(path.exists())

    def test_tariff_rejects_float_bool_nonfinite_negative_and_missing_amounts(self):
        for field in ("generation_input_per_million", "generation_output_per_million", "generation_flat",
                      "count_input_per_million", "count_flat"):
            for value in (1.0, True, Decimal("1"), "NaN", "Infinity", "-1", "", None):
                with self.subTest(field=field, value=repr(value)):
                    with self.assertRaises(PilotBlocked) as caught:
                        self.gate(tariff=fixture_tariff(**{field: value}))
                    self.assertEqual(caught.exception.code, "invalid_money")

    def test_unverified_missing_count_basis_or_wrong_model_prices_are_closed(self):
        variants = [fixture_tariff(count_billing_basis="unknown"), fixture_tariff(model="other-model"),
                    fixture_tariff(provenance={"kind": "synthetic", "source": "fixture", "verified": False}),
                    fixture_tariff(provenance={"kind": "synthetic", "source": "", "verified": True})]
        for tariff in variants:
            with self.subTest(tariff=repr(tariff)):
                with self.assertRaises(PilotBlocked) as caught:
                    self.gate(tariff=tariff)
                self.assertEqual(caught.exception.code, "unverified_tariff")

    def test_variable_count_fee_is_rejected_before_any_transport_can_run(self):
        for rate in ("0.2", "1000000"):
            with self.subTest(rate=rate):
                path = self.root / ("variable-count-" + rate + ".json")
                with self.assertRaises(PilotBlocked) as caught:
                    self.gate(path=path, tariff=fixture_tariff(count_input_per_million=rate))
                self.assertEqual(caught.exception.code, "unverified_tariff")
                self.assertFalse(path.exists())

    def test_valid_live_metadata_is_still_disabled_and_approval_mismatch_is_closed(self):
        tariff = fixture_tariff(provenance={"kind": "trusted_provider", "source": "synthetic_verified_fixture",
                                           "verified": True})
        limits = PilotLimits()
        approval = {"status": "approved", "project": "Indeces", "model": MODEL,
                    "approval_id": "synthetic-disabled-approval", "project_id": "synthetic-project",
                    "guild_id": "10", "channel_id": "20", "config_path": str(Path(__file__).resolve()),
                    "config_source_sha256": CANDIDATE_HASH, "allowed_response_models": [MODEL],
                    "account_binding_confirmed": True, "background_knowledge_disabled": True, "delivery_mode": "api",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "account_id": "synthetic-account", "owner_evidence": "synthetic-owner-evidence",
                    "allowed_stages": ["reply"], "input_bundle_sha256": BUNDLE_HASH,
                    "candidate_identity": CANDIDATE_HASH, "limits": asdict(limits),
                    "tariff_sha256": canonical_hash(asdict(tariff))}
        for supplied, expected in ((None, "approval_missing_or_mismatched"),
                                   ({**approval, "allowed_stages": ["reply", "label"]}, "approval_missing_or_mismatched"),
                                   (approval, "live_disabled")):
            with self.subTest(expected=expected):
                with self.assertRaises(PilotBlocked) as caught:
                    self.gate(mode="live", tariff=tariff, approval=supplied)
                self.assertEqual(caught.exception.code, expected)
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_limits_cannot_expand_tokens_calls_money_or_use_booleans(self):
        for changes in ({"input_tokens": 16385}, {"output_tokens": 2049}, {"max_count_requests": 3},
                        {"max_generations": 3}, {"max_generations": True}, {"hard_cap_usd": "1.01"},
                        {"hard_cap_usd": "0"}, {"hard_cap_usd": float("nan")}):
            with self.subTest(changes=repr(changes)):
                with self.assertRaises(PilotBlocked):
                    self.gate(limits=replace(PilotLimits(), **changes))

    def test_reservation_is_durable_before_response_and_covers_worst_case_caps(self):
        gate = self.gate()
        count = self.admit(gate)
        persisted = json.loads(gate.path.read_text(encoding="utf-8"))
        row = persisted["requests"][0]
        self.assertEqual(row["reservation_id"], count)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["actual_cost_usd"])
        self.assertEqual(Decimal(row["reserved_usd"]), gate.tariff.cost("count", 16384))
        actual = self.settle(gate, count)
        self.assertEqual(Decimal(gate.ledger["requests"][0]["released_usd"]),
                         Decimal(row["reserved_usd"]) - actual)
        generation = self.admit(gate, endpoint=GENERATION_ENDPOINT)
        row = gate.ledger["requests"][-1]
        self.assertEqual(Decimal(row["reserved_usd"]), gate.tariff.cost("generation", 16384, 2048))
        self.settle(gate, generation, out=9)

    def test_two_items_get_exactly_two_counts_and_generations_then_third_is_blocked(self):
        gate = self.gate()
        self.complete_item(gate, "item-1")
        self.complete_item(gate, "item-2")
        self.assertEqual([row["kind"] for row in gate.ledger["requests"]],
                         ["count", "generation", "count", "generation"])
        spent = sum((Decimal(row["actual_cost_usd"]) for row in gate.ledger["requests"]), Decimal(0))
        self.assertLessEqual(spent, Decimal("1"))
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(gate, "item-3")
        self.assertEqual(caught.exception.code, "request_limit")
        self.assertEqual(len(gate.ledger["requests"]), 4)

    def test_money_cap_blocks_admission_before_any_request_is_pending(self):
        gate = self.gate(tariff=fixture_tariff(count_flat="1.01"))
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(gate)
        self.assertEqual(caught.exception.code, "money_cap")
        self.assertEqual(gate.ledger["requests"], [])

    def test_generation_without_count_retry_and_concurrent_pending_are_blocked(self):
        gate = self.gate()
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(gate, endpoint=GENERATION_ENDPOINT)
        self.assertEqual(caught.exception.code, "count_required")
        gate = self.gate()
        token = self.admit(gate)
        self.settle(gate, token)
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(gate)
        self.assertEqual(caught.exception.code, "retry_forbidden")
        gate = self.gate()
        self.admit(gate)
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(gate, "item-2")
        self.assertEqual(caught.exception.code, "request_already_pending")
        self.assertEqual(len(gate.ledger["requests"]), 1)

    def test_exclusive_ledger_lock_rejects_another_writer(self):
        gate = self.gate()
        with self.assertRaises(PilotBlocked) as caught:
            self.gate(path=gate.path)
        self.assertEqual(caught.exception.code, "ledger_locked")

    def test_closed_gate_cannot_write_after_another_owner_acquires_the_ledger(self):
        closed = self.gate()
        path = closed.path
        closed.close()
        active = self.gate(path=path)
        token = self.admit(active, "item-2")
        self.settle(active, token)
        persisted = path.read_bytes()
        with self.assertRaises(PilotBlocked):
            self.admit(closed, "item-1")
        with self.assertRaises(PilotBlocked):
            closed.settle("synthetic-invalid-token", status="failed")
        self.assertEqual(path.read_bytes(), persisted)
        self.assertEqual(len(active.ledger["requests"]), 1)

    def test_close_serializes_with_in_flight_durable_admission_before_releasing_owner_lock(self):
        gate = self.gate()
        save_entered, release_save = threading.Event(), threading.Event()
        close_started, close_finished = threading.Event(), threading.Event()
        errors = []
        original_save = gate._save

        def paused_save():
            save_entered.set()
            if not release_save.wait(2):
                raise AssertionError("Synthetic admission synchronization timed out")
            original_save()

        def admit_request():
            try:
                self.admit(gate)
            except BaseException as error:
                errors.append(error)

        def close_owner():
            close_started.set()
            try:
                gate.close()
            except BaseException as error:
                errors.append(error)
            finally:
                close_finished.set()

        admission = threading.Thread(target=admit_request, daemon=True)
        closer = threading.Thread(target=close_owner, daemon=True)
        with patch.object(gate, "_save", side_effect=paused_save):
            try:
                admission.start()
                self.assertTrue(save_entered.wait(1))
                closer.start()
                self.assertTrue(close_started.wait(1))
                self.assertFalse(close_finished.wait(0.05))
                with self.assertRaises(PilotBlocked) as caught:
                    self.gate(path=gate.path)
                self.assertEqual(caught.exception.code, "ledger_locked")
            finally:
                release_save.set()
                admission.join(3)
                if closer.ident is not None:
                    closer.join(3)
        self.assertFalse(admission.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(close_finished.is_set())
        self.assertEqual(json.loads(gate.path.read_text(encoding="utf-8"))["requests"][0]["status"], "pending")
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(gate, "item-2")
        self.assertEqual(caught.exception.code, "gate_closed")
        reopened = self.gate(path=gate.path)
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(reopened, "item-2")
        self.assertEqual(caught.exception.code, "pending_usage_or_billing_unknown")

    def test_disk_failure_after_failed_receipt_persists_a_sticky_stop_on_reopen(self):
        gate = self.gate()
        token = self.admit(gate)
        original_save = gate._save

        def persisted_then_failed():
            original_save()
            raise OSError("synthetic post-write failure")

        with patch.object(gate, "_save", side_effect=persisted_then_failed):
            with self.assertRaises(OSError):
                self.settle(gate, token, status="failed")
        path = gate.path
        gate.close()
        reopened = self.gate(path=path)
        self.assertEqual(reopened.ledger["requests"][0]["status"], "failed")
        with self.assertRaises(PilotBlocked):
            self.admit(reopened, "item-2")
        self.assertEqual(len(reopened.ledger["requests"]), 1)

    def test_pending_request_on_reopen_blocks_all_remaining_admissions(self):
        gate = self.gate()
        token = self.admit(gate)
        path = gate.path
        gate.close()
        reopened = self.gate(path=path)
        self.assertEqual(reopened.ledger["stop_code"], "pending_usage_or_billing_unknown")
        self.assertEqual(reopened.ledger["requests"][0]["reservation_id"], token)
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(reopened, "item-2")
        self.assertEqual(caught.exception.code, "pending_usage_or_billing_unknown")
        self.assertEqual(len(reopened.ledger["requests"]), 1)

    def test_unknown_usage_or_bill_stops_and_remains_stopped_after_reopen(self):
        for failure in ("usage", "billing"):
            with self.subTest(failure=failure):
                gate = self.gate()
                token = self.admit(gate)
                with self.assertRaises(PilotBlocked) as caught:
                    gate.settle(token, status="completed",
                        usage=None if failure == "usage" else {"input_tokens": 40, "output_tokens": 0},
                        billing=None)
                expected = "usage_unknown" if failure == "usage" else "billing_unknown"
                self.assertEqual(caught.exception.code, expected)
                path = gate.path
                gate.close()
                reopened = self.gate(path=path)
                with self.assertRaises(PilotBlocked) as caught:
                    self.admit(reopened, "item-2")
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(len(reopened.ledger["requests"]), 1)

    def test_confirmed_failed_request_keeps_known_cost_without_retry_or_refund(self):
        gate = self.gate()
        token = self.admit(gate)
        with self.assertRaises(PilotBlocked) as caught:
            self.settle(gate, token, status="failed")
        self.assertEqual(caught.exception.code, "request_failed_no_retry")
        row = gate.ledger["requests"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(Decimal(row["actual_cost_usd"]), gate.tariff.cost("count", 40))
        self.assertIsNotNone(row["usage"])
        with self.assertRaises(PilotBlocked):
            self.admit(gate, "item-2")
        self.assertEqual(len(gate.ledger["requests"]), 1)

    def test_provider_usage_exceeding_reserved_caps_is_logged_and_stops_next_request(self):
        for endpoint, inp, out in ((COUNT_ENDPOINT, 16385, 0), (GENERATION_ENDPOINT, 16384, 2049)):
            with self.subTest(endpoint=endpoint):
                gate = self.gate()
                if endpoint == GENERATION_ENDPOINT:
                    self.settle(gate, self.admit(gate))
                token = self.admit(gate, endpoint=endpoint)
                with self.assertRaises(PilotBlocked) as caught:
                    self.settle(gate, token, inp=inp, out=out)
                self.assertEqual(caught.exception.code, "reservation_breached")
                row = gate.ledger["requests"][-1]
                self.assertEqual(row["usage"], {"input_tokens": inp, "output_tokens": out})
                self.assertEqual(Decimal(row["actual_cost_usd"]), gate.tariff.cost(row["kind"], inp, out))
                if endpoint == GENERATION_ENDPOINT:
                    self.assertGreater(Decimal(row["actual_cost_usd"]), Decimal(row["reserved_usd"]))
                before = len(gate.ledger["requests"])
                with self.assertRaises(PilotBlocked):
                    self.admit(gate, "item-2")
                self.assertEqual(len(gate.ledger["requests"]), before)

    def test_small_exact_decimal_fees_remain_readable_after_settlement_and_reopen(self):
        tariff = fixture_tariff(count_flat="0.0000002")
        gate = self.gate(tariff=tariff)
        token = self.admit(gate)
        self.settle(gate, token, inp=1)
        self.assertEqual(Decimal(gate.ledger["requests"][0]["actual_cost_usd"]), Decimal("0.0000002"))
        path = gate.path
        gate.close()
        reopened = self.gate(path=path, tariff=tariff)
        token = self.admit(reopened, "item-2")
        self.settle(reopened, token, inp=1)

    def test_stage_endpoint_and_ledger_identity_changes_fail_closed(self):
        for stage, endpoint, expected in (("summary", COUNT_ENDPOINT, "stage_forbidden"),
                                          ("label", GENERATION_ENDPOINT, "stage_forbidden"),
                                          ("reply", "https://unapproved.invalid/responses", "endpoint_forbidden")):
            with self.subTest(stage=stage, endpoint=endpoint):
                gate = self.gate()
                with self.assertRaises(PilotBlocked) as caught:
                    self.admit(gate, endpoint=endpoint, stage=stage)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(gate.ledger["requests"], [])
        gate = self.gate()
        path = gate.path
        gate.close()
        with self.assertRaises(PilotBlocked) as caught:
            self.gate(path=path, tariff=fixture_tariff(count_flat="0.002"))
        self.assertEqual(caught.exception.code, "ledger_identity_mismatch")

    def test_ledger_contains_request_provenance_and_costs_without_raw_prompts_or_keys(self):
        gate = self.gate()
        self.complete_item(gate, "item-1")
        text = gate.path.read_text(encoding="utf-8")
        self.assertNotIn("synthetic payload identity", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("api_key", text)
        for row in json.loads(text)["requests"]:
            self.assertEqual(row["payload_sha256"], PAYLOAD_HASH)
            self.assertEqual(row["stage"], "reply")
            self.assertEqual(len(row["reservation_id"]), 32)
            self.assertIn("actual_cost_usd", row)
            self.assertIn("reserved_usd", row)


class PilotLauncherTests(PilotFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_pilot_fixture()

    def tearDown(self):
        self.teardown_pilot_fixture()

    @staticmethod
    def mock_receipt(endpoint, *, count_input=40, missing_usage=False):
        count = endpoint == COUNT_ENDPOINT
        usage = {"input_tokens": count_input, "output_tokens": 0 if count else 9}
        response = {"model": MODEL, "input_tokens": count_input} if count else {
            "model": MODEL, "id": "synthetic-response", "status": "completed", "usage": usage,
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text",
                "text": "Synthetic evidence available [M1]."}]}]}
        if missing_usage and not count:
            response.pop("usage")
        return {"status": "completed", "usage": None if missing_usage else usage,
                "billing": {"status": "known", "verified": True, "source": "synthetic_provider_receipt",
                    "amount_usd": format(synthetic_tariff().cost("count" if count else "generation",
                                                                 usage["input_tokens"], usage["output_tokens"]), "f")},
                "payload": response}

    async def test_finite_runtime_mock_reserves_before_post_and_starts_no_services_or_workers(self):
        sys.path.insert(0, str(DEFAULT_CANDIDATE))
        output = self.root / "finite-runtime"
        pending_receipts = []

        def transport(endpoint, payload, reservation_id):
            persisted = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
            row = persisted["requests"][-1]
            self.assertEqual(row["reservation_id"], reservation_id)
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["actual_cost_usd"])
            pending_receipts.append(reservation_id)
            return self.mock_receipt(endpoint)

        with ExitStack() as guards:
            forbidden = []
            for target in ("indeces.console.serve", "indeces.discord_bridge.DiscordBridge.__init__",
                           "indeces.knowledge.KnowledgeService.__init__", "indeces.knowledge.KnowledgeService.start",
                           "indeces.knowledge.KnowledgeService.scan_once", "indeces.knowledge.KnowledgeService._watch",
                           "indeces.knowledge.KnowledgeService._label_worker", "indeces.knowledge.KnowledgeService._convert_worker",
                           "asyncio.create_task"):
                forbidden.append(guards.enter_context(patch(target, side_effect=AssertionError("Forbidden background lifecycle"))))
            result = await run_mock(output, candidate_root=DEFAULT_CANDIDATE, handler=transport)
            self.assertTrue(all(mock.call_count == 0 for mock in forbidden))
        self.assertEqual(result["mode"], "mock")
        self.assertFalse(result["live_enabled"])
        self.assertEqual((result["count_requests"], result["generation_requests"], result["request_count"]), (2, 2, 4))
        self.assertEqual(len(pending_receipts), 4)
        self.assertEqual(result["delivery_counts"], [1, 1])
        self.assertIsNone(result["stop_code"])
        self.assertEqual(result["verify_runs"]["counts"]["complete"], 2, result["verify_runs"])
        self.assertEqual(result["verify_runs"]["issues"], [])
        self.assertTrue(result["acceptance_passed"])
        self.assertEqual(result["background_workers_started"], 0)
        self.assertEqual(result["real_requests"], 0)
        self.assertEqual(result["blocked_network_attempts"], 0)
        ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
        self.assertTrue(all(row["stage"] == "reply" and row["status"] == "completed" for row in ledger["requests"]))
        EVIDENCE["launcher_mock_count_requests"] += result["count_requests"]
        EVIDENCE["launcher_mock_generation_requests"] += result["generation_requests"]
        EVIDENCE["launcher"] = {"completed_items": result["completed_items"],
            "count_requests": result["count_requests"], "generation_requests": result["generation_requests"],
            "verify_runs_complete": result["verify_runs"]["counts"]["complete"],
            "candidate_source_identity": result["source_identity"], "input_bundle_sha256": result["input_bundle_sha256"],
            "durable_pre_request_reservations": len(pending_receipts), "forbidden_lifecycle_calls": 0}

    async def test_unknown_generation_usage_stops_before_second_item_without_retry(self):
        def transport(endpoint, payload, reservation):
            return self.mock_receipt(endpoint, missing_usage=endpoint == GENERATION_ENDPOINT)

        result = await run_mock(self.root / "unknown-usage", handler=transport)
        self.assertEqual((result["count_requests"], result["generation_requests"]), (1, 1))
        self.assertEqual(result["stop_code"], "usage_unknown")
        self.assertEqual(result["completed_items"], [])
        self.assertFalse(result["acceptance_passed"])
        EVIDENCE["launcher_mock_count_requests"] += result["count_requests"]
        EVIDENCE["launcher_mock_generation_requests"] += result["generation_requests"]

    async def test_known_usage_but_incomplete_generation_stops_before_second_item_and_keeps_bill(self):
        def transport(endpoint, payload, reservation):
            result = self.mock_receipt(endpoint)
            if endpoint == GENERATION_ENDPOINT:
                # The transport returned a known bill, but the response is not
                # a usable completed generation. It cannot admit item two.
                result["payload"]["status"] = "incomplete"
            return result

        result = await run_mock(self.root / "known-incomplete", handler=transport)
        self.assertEqual((result["count_requests"], result["generation_requests"]), (1, 1))
        self.assertIsNotNone(result["stop_code"])
        self.assertEqual(result["completed_items"], [])
        self.assertFalse(result["acceptance_passed"])
        ledger = json.loads(Path(result["ledger_path"]).read_text(encoding="utf-8"))
        self.assertEqual(ledger["requests"][-1]["usage"], {"input_tokens": 40, "output_tokens": 9})
        self.assertEqual(Decimal(ledger["requests"][-1]["actual_cost_usd"]),
                         synthetic_tariff().cost("generation", 40, 9))
        self.assertEqual(len(ledger["requests"]), 2)
        EVIDENCE["launcher_mock_count_requests"] += result["count_requests"]
        EVIDENCE["launcher_mock_generation_requests"] += result["generation_requests"]

    async def test_reply_only_wrapper_rejects_label_and_summary_before_inner_adapter(self):
        class Inner:
            calls = []

            async def call(self, *args, **kwargs):
                self.calls.append(args)
                raise AssertionError("Forbidden stage reached inner adapter")

        for stage in ("label", "summary"):
            with self.subTest(stage=stage):
                gate = self.gate()
                inner = Inner()
                wrapped = ReplyOnlyAdapter(inner, gate)
                wrapped.item_id = "item-1"
                with self.assertRaises(PilotBlocked) as caught:
                    await wrapped.call(stage, "synthetic instructions", [], "synthetic-trace")
                self.assertEqual(caught.exception.code, "stage_forbidden")
                self.assertEqual(inner.calls, [])
                self.assertEqual(gate.ledger["requests"], [])

    async def test_cancellation_after_known_count_keeps_cost_and_blocks_next_item_after_reopen(self):
        gate = self.gate()
        fixture = self

        class CancelledInner:
            async def call(self, *args, **kwargs):
                token = fixture.admit(gate)
                fixture.settle(gate, token)
                raise asyncio.CancelledError

        wrapped = ReplyOnlyAdapter(CancelledInner(), gate)
        wrapped.item_id = "item-1"
        with self.assertRaises(asyncio.CancelledError):
            await wrapped.call("reply", "synthetic instructions", [], "synthetic-cancel-trace")
        self.assertIsNone(_REPLY_CONTEXT.get())
        self.assertEqual(gate.ledger["stop_code"], "reply_cancelled_no_retry")
        self.assertEqual(gate.ledger["requests"][0]["status"], "completed")
        self.assertEqual(Decimal(gate.ledger["requests"][0]["actual_cost_usd"]), gate.tariff.cost("count", 40))
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(gate, "item-2")
        self.assertEqual(caught.exception.code, "reply_cancelled_no_retry")
        path = gate.path
        gate.close()
        reopened = self.gate(path=path)
        with self.assertRaises(PilotBlocked) as caught:
            self.admit(reopened, "item-2")
        self.assertEqual(caught.exception.code, "reply_cancelled_no_retry")
        self.assertEqual(len(reopened.ledger["requests"]), 1)

    async def test_direct_post_without_reply_context_or_changed_prompt_never_reaches_handler(self):
        gate = self.gate()
        called = []
        session = GatedMockSession(gate, lambda *args: called.append(args))
        with self.assertRaises(PilotBlocked) as caught:
            session.post(COUNT_ENDPOINT, json={"model": MODEL, "input": [], "instructions": "synthetic"})
        self.assertEqual(caught.exception.code, "stage_forbidden")
        self.assertEqual(called, [])
        for mutation in ("instructions", "output_schema"):
            with self.subTest(mutation=mutation):
                gate = self.gate(tariff=synthetic_tariff())
                session = GatedMockSession(gate)
                default_handler = session.handler
                handler_calls = []

                def recorded_handler(*args):
                    handler_calls.append(args[0])
                    return default_handler(*args)

                session.handler = recorded_handler
                context = _REPLY_CONTEXT.set(("item-1", "reply"))
                try:
                    payload = {"model": MODEL, "input": [{"role": "user", "content": "synthetic"}],
                               "instructions": "synthetic instructions", "reasoning": {"effort": "medium"},
                               "text": {"verbosity": "high"}}
                    session.post(COUNT_ENDPOINT, json=payload)
                    generation = {**payload, "max_output_tokens": 2048, "store": False, "stream": False,
                                  "truncation": "disabled", "tools": []}
                    if mutation == "instructions":
                        generation["instructions"] = "changed synthetic instructions"
                    else:
                        generation["text"] = {"verbosity": "high", "format": {"type": "json_schema",
                            "name": "synthetic_schema", "strict": True, "schema": {"type": "object",
                                "additionalProperties": False, "properties": {"text": {"type": "string"}},
                                "required": ["text"]}}}
                    with self.assertRaises(PilotBlocked) as caught:
                        session.post(GENERATION_ENDPOINT, json=generation)
                    self.assertEqual(caught.exception.code, "count_prompt_mismatch")
                    self.assertEqual(len(session.requests), 1)
                    self.assertEqual(handler_calls, [COUNT_ENDPOINT])
                    EVIDENCE["direct_post_mock_count_requests"] += 1
                finally:
                    _REPLY_CONTEXT.reset(context)

    async def test_live_cli_without_approved_fees_exits_before_transport_or_state(self):
        output = self.root / "live-disabled"
        with redirect_stdout(io.StringIO()) as printed:
            code = main(["--live", "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(printed.getvalue())["real_requests"], 0)
        self.assertFalse(output.exists())

    async def test_third_input_item_is_rejected_before_ledger_or_runtime_creation(self):
        bundle = synthetic_inputs()
        bundle["questions"].append({"item_id": "synthetic-3", "question": "synthetic excess item"})
        output = self.root / "third-item"
        with self.assertRaises(PilotBlocked) as caught:
            await run_mock(output, bundle)
        self.assertEqual(caught.exception.code, "invalid_input_bundle")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
