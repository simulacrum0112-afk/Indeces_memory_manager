"""Synthetic transport receipts for narrowed cumulative input admission."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig
from indeces.contracts import GovernedError
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces.run_records import _validate_call, _validate_partial_call
from indeces.store import Store


class Scratch:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append((event, deepcopy(fields)))


class RemainingInputRecordTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, *, input_limit=12, counted=10, used=10):
        scratch = Scratch()
        config = AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {"label": Budget(100, 20, 1)})

        async def request(path, payload):
            if path == "/responses/input_tokens":
                return {"input_tokens": counted}
            return {"id": "synthetic-response", "status": "completed",
                    "usage": {"input_tokens": used, "output_tokens": 4},
                    "output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "synthetic labels"}]}]}

        adapter = OpenAIAdapter(config, scratch, request=request)
        error = None
        try:
            await adapter.call("label", "instructions", [{"role": "user", "content": "source"}],
                               "trace", input_limit=input_limit)
        except GovernedError as caught:
            error = caught
        finally:
            await adapter.close()
        return scratch.events, error

    @staticmethod
    def fields(events, name):
        return next(fields for event, fields in events if event == name)

    async def test_actual_smaller_gate_is_complete_with_unchanged_stage_budget(self):
        events, error = await self.capture()
        self.assertIsNone(error)
        self.assertEqual(_validate_call(events), "complete")
        self.assertEqual(self.fields(events, "call_start")["budget"]["input_tokens"], 100)
        self.assertEqual(self.fields(events, "input_gate")["limit"], 12)

    async def test_prior_stage_only_receipts_remain_compatible(self):
        events, error = await self.capture(input_limit=None)
        self.assertIsNone(error)
        start = self.fields(events, "call_start")
        start.pop("requested_input_limit")
        start.pop("effective_input_limit")
        self.fields(events, "input_gate").pop("stage_limit")
        self.assertEqual(_validate_call(events), "complete")

    async def test_larger_requested_allowance_is_bound_to_original_stage_cap(self):
        events, error = await self.capture(input_limit=1000)
        self.assertIsNone(error)
        self.assertEqual(_validate_call(events), "complete")
        changed = deepcopy(events)
        self.fields(changed, "call_start")["effective_input_limit"] = 1000
        self.fields(changed, "input_gate")["limit"] = 1000
        with self.assertRaisesRegex(ValueError, "effective input allowance"):
            _validate_call(changed)

    async def test_effective_cap_must_match_requested_allowance_and_stage(self):
        original, _ = await self.capture()
        mutations = (("effective_input_limit", 13), ("requested_input_limit", 11),
                     ("requested_input_limit", None), ("effective_input_limit", True),
                     ("requested_input_limit", 0))
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                changed = deepcopy(original)
                self.fields(changed, "call_start")[key] = value
                with self.assertRaises(ValueError):
                    _validate_call(changed)
        for key in ("effective_input_limit", "requested_input_limit"):
            with self.subTest(missing=key):
                changed = deepcopy(original)
                self.fields(changed, "call_start").pop(key)
                with self.assertRaisesRegex(ValueError, "declaration incomplete"):
                    _validate_call(changed)

    async def test_gate_cannot_replace_effective_or_configured_stage_cap(self):
        original, _ = await self.capture()
        for key, value in (("limit", 100), ("stage_limit", 101), ("stage_limit", True)):
            with self.subTest(key=key):
                changed = deepcopy(original)
                self.fields(changed, "input_gate")[key] = value
                with self.assertRaisesRegex(ValueError, "effective input budget gate"):
                    _validate_call(changed)

    async def test_input_denial_is_valid_failed_record_without_generation(self):
        events, error = await self.capture(counted=13)
        self.assertEqual(error.code, "input_token_limit")
        self.assertEqual(_validate_call(events), "failed")
        self.assertFalse(any(event == "http_request" and fields["path"] == "/responses"
                             for event, fields in events))
        changed = deepcopy(events)
        self.fields(changed, "input_gate")["limit"] = 13
        self.fields(changed, "input_gate")["admitted"] = True
        with self.assertRaisesRegex(ValueError, "effective input budget gate"):
            _validate_call(changed)
        changed = deepcopy(events)
        self.fields(changed, "input_gate")["input_tokens"] = 11
        self.fields(changed, "input_gate")["admitted"] = True
        with self.assertRaisesRegex(ValueError, "effective input count receipt"):
            _validate_call(changed)

    async def test_actual_breach_usage_is_retained_but_cannot_be_claimed_complete(self):
        events, error = await self.capture(used=13)
        self.assertEqual(error.code, "provider_token_limit_breach")
        self.assertEqual(error.known_usage, {"input_tokens": 13, "output_tokens": 4})
        self.assertFalse(error.remote_usage_unknown)
        self.assertEqual(_validate_call(events), "failed")
        end = self.fields(events, "call_end")
        self.assertEqual(end["known_usage"], error.known_usage)
        end.update(status="completed", result={"text": "synthetic labels", "input_tokens": 13,
                   "output_tokens": 4, "response_id": "synthetic-response", "elapsed_seconds": 0.01})
        with self.assertRaisesRegex(ValueError, "response usage mismatch"):
            _validate_call(events)

    async def test_partial_receipts_bind_surviving_effective_gate_and_usage(self):
        events, _ = await self.capture()
        partial = [(event, fields) for event, fields in events if event != "call_start"]
        self.assertEqual(_validate_partial_call(partial), "retention_partial")
        changed = deepcopy(partial)
        self.fields(changed, "input_gate")["limit"] = 101
        with self.assertRaisesRegex(ValueError, "retained input stage cap"):
            _validate_partial_call(changed)
        changed = deepcopy(partial)
        end = self.fields(changed, "call_end")
        end["result"]["input_tokens"] = 13
        end["known_usage"]["input_tokens"] = 13
        for event, fields in changed:
            if event == "http_response" and fields["path"] == "/responses":
                fields["payload"]["usage"]["input_tokens"] = 13
        with self.assertRaisesRegex(ValueError, "retained input usage cap"):
            _validate_partial_call(changed)

    async def test_real_adapter_breach_usage_is_persisted_once_in_version_accounting(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(name="Indeces", state_dir=root / "state", knowledge_dir=root / "knowledge",
                discord=DiscordConfig("10"), knowledge=KnowledgeConfig(version_input_tokens=100, version_output_tokens=100),
                adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {"label": Budget(100, 20, 1)}))
            scratch, requests = Scratch(), []

            async def request(path, payload):
                requests.append(path)
                if path == "/responses/input_tokens":
                    return {"input_tokens": 10}
                return {"id": "synthetic-breach", "status": "completed",
                        "usage": {"input_tokens": 16, "output_tokens": 4},
                        "output": [{"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": '{"marks":["alpha"]}'}]}]}

            adapter = OpenAIAdapter(config.adapter, scratch, request=request)
            store = Store(config.state_dir)
            try:
                graph = MemoryGraph(store.db)
                service = KnowledgeService(config, store, graph, adapter, scratch)
                (config.knowledge_dir / "paper.txt").write_text("alpha topic source", encoding="utf-8")
                service.scan_once()
                source_id = store.db.execute("SELECT source_id FROM knowledge_desired").fetchone()[0]
                with store.db:
                    store.db.execute("UPDATE knowledge_versions SET input_tokens=85,output_tokens=7 WHERE source_id=?", (source_id,))
                self.assertTrue(await service.label_next())
                self.assertFalse(await service.label_next())
                version = store.db.execute("SELECT status,error,input_tokens,output_tokens FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()
                self.assertEqual(tuple(version), ("failed", "provider_token_limit_breach", 101, 11))
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM knowledge_published").fetchone()[0], 0)
                self.assertEqual(store.db.execute("SELECT marks_json FROM knowledge_chunks WHERE source_id=?", (source_id,)).fetchone()[0], None)
                self.assertEqual(requests, ["/responses/input_tokens", "/responses"])
                self.assertEqual(service.retry_failed()["blocked"][0]["code"], "knowledge_version_token_limit")
                self.assertEqual(store.db.execute("SELECT input_tokens,output_tokens FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()[:], (101, 11))
                calls = [(event, fields) for event, fields in scratch.events if fields.get("call_id")]
                self.assertEqual(_validate_call(calls), "failed")
                self.assertEqual(self.fields(calls, "input_gate")["limit"], 15)
                accounting = self.fields(scratch.events, "label_job_accounting")
                self.assertEqual((accounting["input_tokens"], accounting["output_tokens"]), (101, 11))
            finally:
                await adapter.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
