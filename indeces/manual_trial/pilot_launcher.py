"""Standalone TEST ONLY launcher. No Console, Gateway or knowledge workers."""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout

from .pilot_gate import (COUNT_ENDPOINT, GENERATION_ENDPOINT, MODEL, PilotBlocked,
                        PilotGate, Tariff, canonical_hash, synthetic_tariff)


_REPLY_CONTEXT = ContextVar("finite_pilot_reply_context", default=None)
_MODULE_ROOT = Path(__file__).resolve().parent
DEFAULT_CANDIDATE = (_MODULE_ROOT.parents[1] if _MODULE_ROOT.parent.name == "indeces"
                     else _MODULE_ROOT / "checkpoint-repo")


def candidate_identity(candidate):
    candidate = Path(candidate).resolve()
    if not (candidate / "indeces" / "runtime.py").is_file():
        raise PilotBlocked("candidate_unavailable")
    return canonical_hash({str(path.relative_to(candidate)).replace("\\", "/"):
                           hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted((candidate / "indeces").rglob("*.py"))})


class ReplyOnlyAdapter:
    def __init__(self, adapter, gate):
        self.adapter, self.gate = adapter, gate
        self.item_id = None

    async def call(self, stage, *args, **kwargs):
        if stage != "reply" or self.item_id is None:
            self.gate.halt("stage_forbidden")
        token = _REPLY_CONTEXT.set((self.item_id, stage))
        try:
            try:
                return await self.adapter.call(stage, *args, **kwargs)
            except asyncio.CancelledError:
                if self.gate.ledger["stop_code"] is None:
                    try:
                        self.gate.halt("reply_cancelled_no_retry")
                    except PilotBlocked:
                        pass
                raise
            except Exception:
                if self.gate.ledger["stop_code"] is None:
                    self.gate.halt("reply_failed_no_retry")
                raise
        finally:
            _REPLY_CONTEXT.reset(token)


class MockResponse:
    def __init__(self, payload):
        self.payload, self.status, self.content = payload, 200, self
        self.headers = {"x-request-id": "synthetic-pilot-request"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        yield json.dumps(self.payload).encode()


class GatedMockSession:
    """Direct post admission, not merely an adapter.call budget check."""
    def __init__(self, gate, handler=None):
        self.gate, self.handler = gate, handler or self._default_handler
        self.requests, self.closed, self._counted_prompts = [], False, {}

    def _default_handler(self, endpoint, payload, reservation_id):
        count = endpoint == COUNT_ENDPOINT
        usage = {"input_tokens": 40, "output_tokens": 0 if count else 9}
        kind = "count" if count else "generation"
        billed = format(self.gate.tariff.cost(kind, usage["input_tokens"], usage["output_tokens"]), "f")
        response = {"input_tokens": 40, "model": MODEL} if count else {
            "id": "synthetic-pilot-response", "model": MODEL, "status": "completed", "usage": usage,
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Synthetic evidence available [M1]."}]}]}
        return {"status": "completed", "usage": usage,
                "billing": {"status": "known", "verified": True, "source": "offline_mock_receipt",
                            "amount_usd": billed}, "payload": response}

    def post(self, endpoint, **options):
        context = _REPLY_CONTEXT.get()
        if context is None or context[1] != "reply":
            self.gate.halt("stage_forbidden")
        item_id, stage = context
        if endpoint not in {COUNT_ENDPOINT, GENERATION_ENDPOINT}:
            self.gate.halt("endpoint_forbidden")
        payload = options.get("json")
        common_keys = {"model", "input", "instructions", "reasoning", "text"}
        allowed_keys = common_keys if endpoint == COUNT_ENDPOINT else common_keys | {
            "max_output_tokens", "store", "stream", "truncation", "tools"}
        if (not isinstance(payload, dict) or payload.get("model") != MODEL
                or set(payload) != allowed_keys
                or not isinstance(payload.get("input"), list)
                or not isinstance(payload.get("instructions"), str)
                or payload.get("reasoning") != {"effort": "medium"}
                or not isinstance(payload.get("text"), dict)
                or payload["text"].get("verbosity") != "high"
                or set(payload["text"]) not in ({"verbosity"}, {"verbosity", "format"})):
            self.gate.halt("invalid_request_payload")
        prompt_hash = canonical_hash({key: payload[key] for key in common_keys})
        if endpoint == GENERATION_ENDPOINT:
            maximum = payload.get("max_output_tokens")
            if type(maximum) is not int or not 1 <= maximum <= self.gate.limits.output_tokens:
                self.gate.halt("output_limit")
            if (payload["tools"] != [] or payload["store"] is not False
                    or payload["stream"] is not False or payload["truncation"] != "disabled"):
                self.gate.halt("request_option_forbidden")
            if self._counted_prompts.get(item_id) != prompt_hash:
                self.gate.halt("count_prompt_mismatch")
        reservation = self.gate.admit(item_id, stage, endpoint, canonical_hash(payload))
        # No credential/header/body is stored here. The ledger is already on
        # disk when this injected mock handler runs.
        self.requests.append({"endpoint": endpoint, "item_id": item_id,
                              "payload_sha256": canonical_hash(payload), "reservation_id": reservation})
        try:
            result = self.handler(endpoint, payload, reservation)
        except Exception:
            self.gate.settle(reservation, status="failed")
            raise PilotBlocked("usage_unknown") from None
        if not isinstance(result, dict):
            self.gate.settle(reservation, status="failed")
        response, usage = result.get("payload"), result.get("usage")
        if not isinstance(response, dict) or response.get("model") != MODEL:
            self.gate.settle(reservation, status="failed", usage=usage)
        if endpoint == GENERATION_ENDPOINT and (not isinstance(response, dict) or response.get("usage") != usage):
            self.gate.settle(reservation, status="failed")
        if endpoint == COUNT_ENDPOINT and (not isinstance(response, dict)
                or type(response.get("input_tokens")) is not int
                or not 0 <= response["input_tokens"] <= self.gate.limits.input_tokens
                or not isinstance(usage, dict) or usage.get("input_tokens") != response["input_tokens"]):
            self.gate.settle(reservation, status="failed", usage=usage, billing=result.get("billing"))
        self.gate.settle(reservation, status=result.get("status"), usage=usage, billing=result.get("billing"))
        if endpoint == COUNT_ENDPOINT:
            self._counted_prompts[item_id] = prompt_hash
        return MockResponse(response)

    async def close(self):
        self.closed = True


def synthetic_inputs():
    return {"schema": "indeces_finite_pilot_inputs_v1", "kind": "synthetic",
            "questions": [{"item_id": f"synthetic-{number}", "question": f"alpha topic synthetic item {number}"}
                          for number in (1, 2)],
            "materials": [{"source_id": f"synthetic-source-{number}",
                           "text": (f"alpha topic source {number}: " + "synthetic evidence. " * 30)[:400],
                           "marks": ["alpha", "topic"]} for number in range(1, 7)]}


def validated_inputs(value):
    if (not isinstance(value, dict) or value.get("schema") != "indeces_finite_pilot_inputs_v1"
            or value.get("kind") not in {"synthetic", "approved_frozen"}
            or not isinstance(value.get("questions"), list) or not 1 <= len(value["questions"]) <= 2
            or not isinstance(value.get("materials"), list) or len(value["materials"]) > 128):
        raise PilotBlocked("invalid_input_bundle")
    allowed = {"schema", "kind", "questions", "materials"}
    if value["kind"] == "approved_frozen":
        allowed.add("owner_evidence")
    if set(value) != allowed:
        raise PilotBlocked("invalid_input_bundle")
    if value["kind"] == "approved_frozen" and (not isinstance(value.get("owner_evidence"), str)
                                               or not value["owner_evidence"].strip()):
        raise PilotBlocked("unapproved_input_bundle")
    ids = []
    for question in value["questions"]:
        if (not isinstance(question, dict) or set(question) != {"item_id", "question"}
                or not isinstance(question["item_id"], str)
                or not isinstance(question["question"], str) or not 1 <= len(question["question"]) <= 8192):
            raise PilotBlocked("invalid_input_bundle")
        ids.append(question["item_id"])
    if len(set(ids)) != len(ids):
        raise PilotBlocked("invalid_input_bundle")
    for material in value["materials"]:
        if (not isinstance(material, dict) or set(material) != {"source_id", "text", "marks"}
                or not isinstance(material["source_id"], str) or not material["source_id"]
                or not isinstance(material["text"], str) or not 1 <= len(material["text"]) <= 400
                or not isinstance(material["marks"], list) or not 1 <= len(material["marks"]) <= 8
                or any(not isinstance(mark, str) or not 1 <= len(mark) <= 40 for mark in material["marks"])):
            raise PilotBlocked("invalid_input_bundle")
    return value


async def run_mock(output_dir, inputs=None, *, candidate_root=DEFAULT_CANDIDATE, handler=None):
    bundle = validated_inputs(synthetic_inputs() if inputs is None else inputs)
    candidate = Path(candidate_root).resolve()
    if not (candidate / "indeces" / "runtime.py").is_file():
        raise PilotBlocked("candidate_unavailable")
    sys.path.insert(0, str(candidate))
    from indeces.adapter import OpenAIAdapter
    from indeces.config import AdapterConfig, Budget, DiscordConfig, RuntimeConfig
    from indeces.contracts import DeliveryReceipt, IncomingMessage
    from indeces.memory import MemoryGraph
    from indeces.run_records import verify_runs
    from indeces.runtime import Runtime
    from indeces.scratch import ScratchLog
    from indeces.selection_policy import RankedTopThreeSelector
    from indeces.store import Store
    for name in ("adapter", "memory", "memory_index", "runtime", "run_records", "selection_policy", "store"):
        module = sys.modules["indeces." + name]
        if Path(module.__file__).resolve() != candidate / "indeces" / (name + ".py"):
            raise PilotBlocked("candidate_import_identity_mismatch")

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_hash = candidate_identity(candidate)
    gate = PilotGate(output / "ledger.json", tariff=synthetic_tariff(),
                     input_bundle_sha256=canonical_hash(bundle), candidate_identity=source_hash)
    session = GatedMockSession(gate, handler)
    delivery_counts, completed_items, paths = [], [], []
    blocked_network_attempts = 0

    def deny_network(*args, **kwargs):
        nonlocal blocked_network_attempts
        blocked_network_attempts += 1
        raise PilotBlocked("network_disabled")

    try:
        with TemporaryDirectory(prefix="indeces-finite-pilot-") as temporary:
            # Fresh isolated state and a fresh conversation channel per item:
            # no historical summary eligibility, scanners, or copied database.
            root = Path(temporary)
            store = Store(root / "state")
            try:
                config = SimpleNamespace(name="Indeces", discord=DiscordConfig("10"),
                    runtime=replace(RuntimeConfig(), turn_seconds=60),
                    adapter=AdapterConfig(MODEL, "https://api.openai.com/v1", {
                        "reply": Budget(16384, 2048, 45, reasoning="medium"),
                        "summary": Budget(1, 1, 1, reasoning="medium"),
                        "label": Budget(1, 1, 1, reasoning="medium")}, verbosity="high"))
                graph = MemoryGraph(store.db, retrieval_policy=config.runtime.retrieval_policy)
                for material in bundle["materials"]:
                    graph.add("10:knowledge", material["source_id"], "synthetic-frozen-input",
                              [{"text": material["text"], "quote": material["text"], "marks": material["marks"]}], 1)
                scratch = ScratchLog(output / "scratch")
                adapter = OpenAIAdapter(config.adapter, scratch, api_key="synthetic-placeholder")
                adapter._session = session
                reply_only = ReplyOnlyAdapter(adapter, gate)
                runtime = Runtime(config, store, reply_only, scratch, selector=RankedTopThreeSelector())
                try:
                    with patch("socket.create_connection", deny_network), patch.object(socket.socket, "connect", deny_network), \
                            patch.object(socket.socket, "connect_ex", deny_network), redirect_stdout(io.StringIO()):
                        for index, question in enumerate(bundle["questions"]):
                            if gate.ledger["stop_code"]:
                                break
                            reply_only.item_id = question["item_id"]
                            delivery = []

                            async def deliver(text):
                                delivery.append(text)
                                return DeliveryReceipt((f"synthetic-delivery-{index}",), text)

                            message = IncomingMessage(question["item_id"], str(20 + index), "10", "30",
                                "Synthetic Pilot", question["question"], datetime.now(timezone.utc).isoformat())
                            await runtime.process(message, deliver)
                            delivery_counts.append(len(delivery))
                            if gate.ledger["stop_code"]:
                                break
                            generated = any(row["item_id"] == question["item_id"]
                                            and row["kind"] == "generation" and row["status"] == "completed"
                                            for row in gate.ledger["requests"])
                            turn = store.db.execute("SELECT status FROM turns WHERE message_id=?",
                                                    (question["item_id"],)).fetchone()
                            if not generated or turn is None or turn[0] != "delivered":
                                try:
                                    gate.halt("runtime_failed_no_retry")
                                except PilotBlocked:
                                    break
                            completed_items.append(question["item_id"])
                finally:
                    await adapter.close()
                    scratch.close()
                paths = [scratch.path]
                verification = verify_runs(paths)
            finally:
                store.close()
        result = {"schema": "finite_pilot_mock_result_v1", "mode": "mock", "live_enabled": False,
                  "source_identity": source_hash, "input_bundle_sha256": canonical_hash(bundle),
                  "expected_items": len(bundle["questions"]), "delivered_items": list(completed_items),
                  "completed_items": completed_items, "request_count": len(session.requests),
                  "count_requests": sum(row["endpoint"] == COUNT_ENDPOINT for row in session.requests),
                  "generation_requests": sum(row["endpoint"] == GENERATION_ENDPOINT for row in session.requests),
                  "delivery_counts": delivery_counts, "stop_code": gate.ledger["stop_code"],
                  "ledger_path": str(gate.path), "scratch_paths": list(map(str, paths)),
                  "verify_runs": verification, "real_requests": 0,
                  "blocked_network_attempts": blocked_network_attempts,
                  "background_workers_started": 0, "synthetic_tariff_only": True}
        counts, call_counts = verification["counts"], verification["call_counts"]
        result["acceptance_passed"] = (gate.ledger["stop_code"] is None
            and len(completed_items) == len(bundle["questions"])
            and delivery_counts == [1] * len(bundle["questions"])
            and counts["complete"] == len(bundle["questions"])
            and call_counts["complete"] == len(bundle["questions"])
            and all(counts[key] == 0 and call_counts[key] == 0
                    for key in ("failed", "incomplete", "invalid", "retention_partial"))
            and not verification["issues"])
        (output / "RESULT.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result
    finally:
        gate.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "mock-run")
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--candidate-root", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--tariff", type=Path)
    args = parser.parse_args(argv)
    try:
        inputs = json.loads(args.inputs.read_text(encoding="utf-8")) if args.inputs else None
        if args.live:
            if not args.approval or not args.tariff or inputs is None:
                raise PilotBlocked("approval_or_tariff_or_inputs_missing")
            approval = json.loads(args.approval.read_text(encoding="utf-8"))
            tariff = Tariff(**json.loads(args.tariff.read_text(encoding="utf-8")))
            PilotGate(args.output / "ledger.json", mode="live", tariff=tariff, approval=approval,
                      input_bundle_sha256=canonical_hash(validated_inputs(inputs)),
                      candidate_identity=candidate_identity(args.candidate_root))
            raise PilotBlocked("live_disabled")
        result = asyncio.run(asyncio.wait_for(run_mock(args.output, inputs, candidate_root=args.candidate_root), 120))
        print(json.dumps({key: result[key] for key in ("mode", "live_enabled", "request_count",
              "count_requests", "generation_requests", "stop_code", "real_requests", "ledger_path")}, sort_keys=True))
        return 0 if result["acceptance_passed"] else 2
    except PilotBlocked as error:
        print(json.dumps({"status": "blocked", "code": error.code, "real_requests": 0}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "blocked", "code": "invalid_preflight_or_local_failure", "real_requests": 0}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
