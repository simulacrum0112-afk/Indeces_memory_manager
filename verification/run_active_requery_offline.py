"""Bounded synthetic regression; real provider transport is denied.

Usage: python -X utf8 -B verification/run_active_requery_offline.py --phase one
All Store/Scratch fixtures use TemporaryDirectory; no production entry is run.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import time
import unittest
from unittest.mock import patch
import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from indeces.adapter import OpenAIAdapter

PHASES = {
    "one": ["tests.test_active_requery", "tests.test_requery_records",
            "tests.test_model_selection_optional", "tests.test_model_selection",
            "tests.test_run_records", "tests.test_adapter", "tests.test_context",
            "tests.test_remaining_input_records", "tests.test_bot_reply_records",
            "tests.test_local_retrieval_deadline"],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    counters = {"real_provider_attempts_denied": 0, "external_socket_attempts_denied": 0,
                "injected_count_requests": 0, "injected_generation_requests": 0}
    async def no_provider(*args, **kwargs):
        counters["real_provider_attempts_denied"] += 1
        raise AssertionError("Real provider transport denied by offline verification")
    native_post = OpenAIAdapter._post
    async def counted_post(adapter, path, *args, **kwargs):
        if adapter._request_override is not None:
            key = "injected_count_requests" if path == "/responses/input_tokens" else "injected_generation_requests"
            counters[key] += 1
        return await native_post(adapter, path, *args, **kwargs)
    native_connect, native_connect_ex = socket.socket.connect, socket.socket.connect_ex
    def guarded_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            counters["external_socket_attempts_denied"] += 1
            raise AssertionError("External network denied by offline verification")
        return native_connect(sock, address)
    def guarded_connect_ex(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            counters["external_socket_attempts_denied"] += 1
            raise AssertionError("External network denied by offline verification")
        return native_connect_ex(sock, address)
    stream = io.StringIO()
    started = datetime.now(timezone.utc).isoformat()
    clock = time.monotonic()
    with ExitStack() as stack:
        stack.enter_context(patch.object(aiohttp.ClientSession, "_request", no_provider))
        stack.enter_context(patch.object(OpenAIAdapter, "_post", counted_post))
        stack.enter_context(patch.object(socket.socket, "connect", guarded_connect))
        stack.enter_context(patch.object(socket.socket, "connect_ex", guarded_connect_ex))
        stack.enter_context(redirect_stdout(stream))
        suite = unittest.defaultTestLoader.loadTestsFromNames(PHASES[args.phase])
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    receipt = {"schema": "active_requery_offline_verification_v1", "phase": args.phase,
               "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(),
               "elapsed_seconds": time.monotonic() - clock, "modules": PHASES[args.phase],
               "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "successful": result.wasSuccessful(),
               "failed_ids": [t.id() for t, _ in result.failures], "error_ids": [t.id() for t, _ in result.errors],
               "simulation_only": True, "real_model_calls": 0, "provider_fees": 0,
               "production_configuration_or_process_changed": False,
               "existing_question_or_gold_data_read": False, "transport": counters,
               "source_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted((ROOT / "indeces").rglob("*.py"))
                                  if args.phase != "one" or p.name != "communicability.py"},
               "command": f"python -X utf8 -B verification/run_active_requery_offline.py --phase {args.phase}"}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        args.output.with_suffix(".log").write_text(stream.getvalue(), encoding="utf-8")
    print(json.dumps({k: v for k, v in receipt.items() if k != "source_sha256"}, ensure_ascii=True))
    if not result.wasSuccessful():
        for test, detail in result.failures + result.errors:
            print(test.id() + "\n" + detail)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
