"""Free transport controls for the separately approved isolated smoke entry."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from indeces import path_requery_smoke as smoke
from indeces.contracts import GovernedError


def goal_action():
    return {"action": "query", "query": {
        "entities": [
            {"id": "e1", "surface": "alpha", "canonical": "alpha", "span": [0, 5]},
            {"id": "e2", "surface": "gamma", "canonical": "gamma", "span": [12, 17]}],
        "relations": [{"subject_id": "e1", "object_id": "e2", "predicate": "links",
            "surface": "links", "span": [6, 11], "negated": False,
            "direction": "subject_to_object"}],
        "negations": [], "time": None, "scope": None,
        "canonical_terms": ["alpha", "gamma"], "ambiguities": []},
        "missing_evidence": ["Synthetic missing link source"], "clarification": "", "stop_reason": ""}


def done():
    return {"action": "answer", "query": None, "missing_evidence": [],
            "clarification": "", "stop_reason": "evidence_sufficient"}


class SmokeTransport:
    def __init__(self, *, no_query=False, unknown=False):
        self.calls = []
        self.query_index = 0
        self.no_query, self.unknown = no_query, unknown

    async def __call__(self, path, payload):
        stage = payload.get("text", {}).get("format", {}).get("name", "reply")
        self.calls.append((path, stage, payload))
        if path == "/responses/input_tokens":
            return {"input_tokens": 64}
        if path != "/responses":
            raise AssertionError("unexpected mock endpoint")
        if stage == "query":
            if self.unknown:
                raise GovernedError("synthetic_transport_lost")
            value = done() if self.no_query or self.query_index else goal_action()
            self.query_index += 1
            text = json.dumps(value)
        elif stage == "selection":
            request = json.loads(payload["input"][0]["content"])
            text = json.dumps({"selected_record_ids": [item["record_id"] for item in
                request["candidates"][:request["required_selection_count"]]]})
        else:
            text = "Synthetic chain source observations [M3] and [M4]."
        return {"id": "synthetic-smoke-response-" + str(len(self.calls)), "status": "completed",
            "usage": {"input_tokens": 64, "output_tokens": 32},
            "output": [{"type": "message", "role": "assistant", "content": [{
                "type": "output_text", "text": text}]}]}


class PathRequerySmokeTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name).resolve()
        self.addCleanup(self.folder.cleanup)
        # Windows asyncio creates a numeric-loopback self-pipe while main()
        # constructs its event loop. Permit that OS mechanism; block every
        # non-loopback connect and every HTTP ClientSession.
        for method in ("connect", "connect_ex"):
            original = getattr(socket.socket, method)
            def guarded(sock, address, _original=original):
                if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1"):
                    raise AssertionError("external network forbidden")
                return _original(sock, address)
            guard = patch("socket.socket." + method, guarded)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch("aiohttp.ClientSession", side_effect=AssertionError("external HTTP forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def invoke(self, argv, **kwargs):
        output = StringIO()
        with redirect_stdout(output):
            code = smoke.main(argv, **kwargs)
        return code, json.loads(output.getvalue().splitlines()[-1])

    def approved(self, run_dir=None, *, cost="1"):
        return ["--execute", "--run-dir", str(run_dir or self.root / "new-run"),
            "--approved-cost-usd", cost, "--input-upper-usd-per-million", "1",
            "--output-upper-usd-per-million", "2"]

    def test_default_plan_reads_no_environment_creates_no_state_or_adapter(self):
        key = Mock(side_effect=AssertionError("credential read forbidden"))
        with patch.object(smoke.os, "environ", {}), patch.object(smoke, "_new_directory",
                side_effect=AssertionError("path inspection forbidden")):
            code, report = self.invoke([], _key_reader=key)
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "dry_run")
        self.assertFalse(report["environment_read"])
        self.assertFalse(report["state_created"])
        self.assertFalse(report["network_issued"])
        self.assertFalse(report["scientific_or_answer_quality_verified"])
        self.assertEqual(list(self.root.iterdir()), [])
        key.assert_not_called()

    def test_dry_run_with_target_and_prices_still_has_no_side_effects(self):
        key = Mock(side_effect=AssertionError("credential read forbidden"))
        args = self.approved()[1:]
        code, report = self.invoke(args, _key_reader=key)
        self.assertEqual(code, 0)
        self.assertEqual(report["configured_token_cap_cost_usd"], "0.04096")
        self.assertEqual(report["admission_cost_ceiling_usd"], "0.04096")
        self.assertFalse((self.root / "new-run").exists())
        key.assert_not_called()

    def test_execute_without_positive_approval_refuses_before_key_and_files(self):
        key = Mock(side_effect=AssertionError("credential read forbidden"))
        code, report = self.invoke(["--execute", "--run-dir", str(self.root / "new-run")], _key_reader=key)
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "invalid_approved_cost_usd")
        self.assertFalse((self.root / "new-run").exists())
        key.assert_not_called()

    def test_nonfinite_nonpositive_and_over_cap_budget_values_refuse(self):
        for value in ("0", "-1", "NaN", "Infinity", "1001", "garbage", "1e-9999"):
            with self.subTest(value=value):
                key = Mock(side_effect=AssertionError("credential read forbidden"))
                code, _ = self.invoke(self.approved(cost=value), _key_reader=key)
                self.assertEqual(code, 2)
                key.assert_not_called()
        self.assertFalse((self.root / "new-run").exists())

    def test_each_unit_price_requires_explicit_positive_admission(self):
        for option in ("--input-upper-usd-per-million", "--output-upper-usd-per-million"):
            for value in ("0", "NaN", "-1", "1001"):
                with self.subTest(option=option, value=value):
                    args = self.approved()
                    args[args.index(option) + 1] = value
                    key = Mock(side_effect=AssertionError("credential read forbidden"))
                    code, _ = self.invoke(args, _key_reader=key)
                    self.assertEqual(code, 2)
                    key.assert_not_called()
        self.assertFalse((self.root / "new-run").exists())

    def test_execute_requires_new_absolute_target_and_existing_parent(self):
        for target in ("relative-smoke-target", self.root / "absent-parent" / "new-run"):
            with self.subTest(target=str(target)):
                key = Mock(side_effect=AssertionError("credential read forbidden"))
                code, _ = self.invoke(self.approved(target), _key_reader=key)
                self.assertEqual(code, 2)
                key.assert_not_called()

    def test_existing_run_refuses_before_environment_or_transport(self):
        target = self.root / "saved-run"
        target.mkdir()
        sentinel = target / "preserved.json"
        sentinel.write_text('{"unknown_usage":true}', encoding="utf-8")
        before = sentinel.read_bytes()
        key = Mock(side_effect=AssertionError("credential read forbidden"))
        transport = Mock(side_effect=AssertionError("transport forbidden"))
        with patch.object(smoke, "_read_key", key):
            code, report = self.invoke(self.approved(target), _request=transport)
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "run_directory_already_exists")
        self.assertEqual(sentinel.read_bytes(), before)
        key.assert_not_called()
        transport.assert_not_called()

    def test_new_target_inside_material_or_management_tree_refuses_key_and_writes(self):
        for number, name in enumerate(("knowledge", "state", "scratch", ".git", ".indeces", "_staging", "KNOWLEDGE")):
            with self.subTest(name=name):
                parent = self.root / ("outer-" + str(number)) / name / "nested"
                parent.mkdir(parents=True)
                sentinel = parent / "synthetic-preserved.txt"
                sentinel.write_text("Synthetic existing original content.", encoding="utf-8")
                before = sentinel.read_bytes()
                target = parent / "new-smoke"
                key = Mock(side_effect=AssertionError("credential read forbidden"))
                transport = Mock(side_effect=AssertionError("transport forbidden"))
                code, report = self.invoke(self.approved(target), _request=transport, _key_reader=key)
                self.assertEqual(code, 2)
                self.assertEqual(report["error"]["code"], "protected_run_directory_ancestor")
                self.assertEqual(sentinel.read_bytes(), before)
                self.assertFalse(target.exists())
                self.assertEqual(list(parent.iterdir()), [sentinel])
                key.assert_not_called()
                transport.assert_not_called()

    def test_dot_traversal_cannot_enter_or_escape_protected_target_ancestors(self):
        ordinary = self.root / "ordinary"
        ordinary.mkdir()
        protected = self.root / "knowledge" / "nested"
        protected.mkdir(parents=True)
        for target in (ordinary / ".." / "knowledge" / "nested" / "new-run",
                       protected / ".." / "new-run",
                       protected / ".." / ".." / "ordinary" / "new-run"):
            with self.subTest(target=str(target)):
                key = Mock(side_effect=AssertionError("credential read forbidden"))
                code, report = self.invoke(self.approved(target), _key_reader=key)
                self.assertEqual(code, 2)
                self.assertEqual(report["error"]["code"], "protected_run_directory_ancestor")
                self.assertFalse(target.exists())
                key.assert_not_called()
        self.assertEqual(list(ordinary.iterdir()), [])
        self.assertEqual(list(protected.iterdir()), [])

    def test_missing_existing_environment_key_refuses_without_creating_target(self):
        key = Mock(return_value="")
        code, report = self.invoke(self.approved(), _key_reader=key)
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "existing_environment_api_key_required")
        self.assertFalse((self.root / "new-run").exists())
        key.assert_called_once_with()

    def test_authorized_shaped_transport_uses_configured_real_ledger_and_bound_feedback(self):
        transport = SmokeTransport()
        code, report = self.invoke(self.approved(), _request=transport,
            _key_reader=lambda: "synthetic-smoke-key-never-persisted")
        self.assertEqual(code, 0)
        self.assertEqual(report["path_loop_observation"], "observed")
        self.assertFalse(report["live_provider_transport"])
        self.assertTrue(report["strict_split_source_rounds_observed"])
        self.assertTrue(report["next_planner_received_bound_feedback"])
        self.assertTrue(report["both_source_bodies_in_final_context"])
        self.assertEqual(report["declared_synthetic_formal_chain_count"], 1)
        self.assertEqual(report["provider_stages"], ["selection", "query", "query", "reply"])
        usage = report["usage"]
        self.assertEqual(usage["mode"], "configured_real")
        self.assertEqual(usage["phase"], "completed")
        self.assertEqual(usage["cost_basis"], "configured_upper_unit_prices_not_invoice")
        self.assertEqual(usage["unknown_generation_count"], 0)
        self.assertTrue(usage["usage_complete"])
        self.assertEqual(usage["automatic_retries"], 0)
        self.assertEqual(usage["limits"]["calls"], 6)
        self.assertEqual(usage["limits"]["input_tokens"], 32768)
        self.assertEqual(usage["limits"]["output_tokens"], 4096)
        self.assertEqual(usage["limits"]["seconds"], 60.0)
        self.assertEqual(report["record_validation"]["issues"], [])
        run_dir = self.root / "new-run"
        self.assertFalse((run_dir / "knowledge").exists())
        for path in run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"synthetic-smoke-key-never-persisted", path.read_bytes(), str(path))
        for path, _, payload in transport.calls:
            self.assertEqual(payload["model"], "gpt-6.1-sol")
            if path == "/responses":
                self.assertEqual(payload["reasoning"]["effort"], "medium")
        key = Mock(side_effect=AssertionError("cannot reopen saved execution"))
        count = len(transport.calls)
        second_code, second_report = self.invoke(self.approved(), _request=transport, _key_reader=key)
        self.assertEqual(second_code, 2)
        self.assertEqual(second_report["error"]["code"], "run_directory_already_exists")
        self.assertEqual(len(transport.calls), count)
        key.assert_not_called()

    def test_actual_model_action_can_skip_requery_without_claiming_path_success(self):
        transport = SmokeTransport(no_query=True)
        code, report = self.invoke(self.approved(), _request=transport, _key_reader=lambda: "synthetic")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["path_loop_observation"], "not_executed")
        self.assertEqual(report["native_requery_rounds"], 0)
        self.assertEqual(report["path_feedback_events"], 0)
        self.assertFalse(report["both_source_bodies_in_final_context"])
        self.assertEqual(report["provider_stages"], ["selection", "query", "reply"])

    def test_unknown_generation_usage_closes_run_and_issues_no_final_model(self):
        transport = SmokeTransport(unknown=True)
        code, report = self.invoke(self.approved(), _request=transport, _key_reader=lambda: "synthetic")
        self.assertEqual(code, 1)
        self.assertEqual(report["path_loop_observation"], "not_executed")
        self.assertEqual(report["provider_stages"], ["selection", "query"])
        self.assertEqual(report["usage"]["halted"], "requery_usage_unknown")
        self.assertEqual(report["usage"]["unknown_generation_count"], 1)
        self.assertFalse(report["usage"]["usage_complete"])
        self.assertEqual(report["usage"]["automatic_retries"], 0)
        self.assertEqual(len(transport.calls), 4)
        self.assertTrue((self.root / "new-run" / "RESULT.json").exists())

    def test_insufficient_reserved_cost_prevents_first_input_count_or_generation(self):
        transport = SmokeTransport()
        code, report = self.invoke(self.approved(cost="0.000001"),
            _request=transport, _key_reader=lambda: "synthetic")
        self.assertEqual(code, 1)
        self.assertEqual(transport.calls, [])
        self.assertEqual(report["usage"]["halted"], "requery_cost_budget")
        self.assertEqual(report["usage"]["unknown_generation_count"], 0)
        self.assertEqual(report["path_loop_observation"], "not_executed")

    def test_cleanup_failure_keeps_known_usage_but_does_not_claim_completed_smoke(self):
        from indeces.adapter import OpenAIAdapter
        async def failed_close(adapter):
            raise GovernedError("synthetic_close_failure")
        transport = SmokeTransport()
        with patch.object(OpenAIAdapter, "close", failed_close):
            code, report = self.invoke(self.approved(), _request=transport,
                _key_reader=lambda: "synthetic")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["path_loop_observation"], "partial")
        self.assertEqual(report["failure"]["code"], "synthetic_close_failure")
        self.assertEqual(report["usage"]["phase"], "completed")
        self.assertTrue(report["usage"]["usage_complete"])
        self.assertEqual(report["usage"]["unknown_generation_count"], 0)


if __name__ == "__main__":
    unittest.main()
