"""Reproducible synthetic checks; a file entry point supports Windows spawn.

Run from the repository: .venv/Scripts/python.exe verification/verify_offline.py
Never starts the real service or reads the user's config/state/scratch.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from indeces import __version__

    output = io.StringIO()
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
    report = {
        "version": __version__, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "python_version": platform.python_version(),
        "live_model_calls": False, "live_discord_connection": False,
        "private_config_or_runtime_read_in_this_test_run": False,
        "private_runtime_data_included": False,
        "service_started_stopped_or_hotpatched": False,
        "scientific_model_or_budget_changes": False,
        "scope": "Synthetic offline fixtures and an isolated example config only.",
        "unit_tests": {
            "runner": "unittest discovery: tests (file entry point with main guard)",
            "tests_run": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skips": len(result.skipped),
            "failed_cases": [str(test) for test, _ in result.failures],
            "error_cases": [str(test) for test, _ in result.errors],
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "passed": result.wasSuccessful(),
            "skipped_cases": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
        }, "checks": [],
    }
    if not result.wasSuccessful():
        print(output.getvalue())
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    with tempfile.TemporaryDirectory(prefix="indeces-offline-check-") as directory:
        config = Path(directory) / "config.example.toml"
        config.write_bytes((ROOT / "config.example.toml").read_bytes())
        commands = [
            ([sys.executable, "-m", "indeces", "check", "--config", str(config)], None),
            ([sys.executable, "-m", "pip", "check"], None),
            ([sys.executable, "-m", "indeces", "--help"], None),
            ([sys.executable, "-m", "indeces", "console", "--config", str(config)], "quit\n"),
            ([sys.executable, "-m", "indeces", "npmi", "--config", str(config)], None),
        ]
        for command, stdin in commands:
            started = time.perf_counter()
            process = subprocess.run(command, cwd=ROOT, input=stdin, text=True,
                                     encoding="utf-8", capture_output=True, env=env, timeout=60)
            report["checks"].append({
                "command": ["python" if item == sys.executable else config.name if item == str(config) else item for item in command],
                "exit_code": process.returncode,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "output": process.stdout.strip(), "stderr": process.stderr.strip(),
            })
    report["passed"] = result.wasSuccessful() and all(check["exit_code"] == 0 for check in report["checks"])
    path = ROOT / "verification" / ("OFFLINE_" + __version__.replace(".", "") + ".json")
    if path.exists():
        path = path.with_name(path.stem + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + path.suffix)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"version": __version__, "report": path.name, "unit_tests": report["unit_tests"],
                      "checks_passed": all(check["exit_code"] == 0 for check in report["checks"]),
                      "passed": report["passed"]}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
