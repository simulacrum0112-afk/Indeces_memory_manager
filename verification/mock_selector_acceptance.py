"""Run the isolated synthetic selector acceptance and emit a machine receipt.

Usage from candidate root:
  python verification/mock_selector_acceptance.py --report verification/MOCK_ACCEPTANCE.json

No service starts, external HTTP, credentials, model calls, or user state are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import indeces.memory
import indeces.memory_index
import indeces.runtime
import indeces.run_records
import indeces.selection_policy
from tests import test_selection_policy_integration as acceptance


class ScenarioResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenarios = {}

    def startTest(self, test):
        self.scenarios[test.id()] = "running"
        super().startTest(test)

    def addSuccess(self, test):
        self.scenarios[test.id()] = "passed"
        super().addSuccess(test)

    def addFailure(self, test, error):
        self.scenarios[test.id()] = "failed"
        super().addFailure(test, error)

    def addError(self, test, error):
        self.scenarios[test.id()] = "error"
        super().addError(test, error)

    def addSubTest(self, test, subtest, error):
        if error:
            self.scenarios[test.id()] = "failed_subtest"
        super().addSubTest(test, subtest, error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    provenance = {}
    for module in (indeces.memory, indeces.memory_index, indeces.runtime, indeces.run_records,
                   indeces.selection_policy, acceptance):
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(ROOT):
            raise RuntimeError("Refusing an import outside the isolated candidate")
        provenance[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    suite = unittest.defaultTestLoader.loadTestsFromModule(acceptance)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=ScenarioResult).run(suite)
    report = {"schema": "selector_mock_acceptance_v1", "candidate_root": str(ROOT),
              "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "skipped": len(result.skipped), "passed": result.wasSuccessful(),
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "source_sha256": provenance, "scenarios": result.scenarios,
              "evidence": acceptance.ACCEPTANCE_EVIDENCE,
              "limits": {"production_state_writes": 0, "real_model_calls": 0,
                         "real_discord_calls": 0, "production_index_migrations": 0,
                         "model_quality_evaluation": "not_run",
                         "default_strategy_effect_improvement": "not_claimed"}}
    serialized = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        path = args.report.resolve()
        if not path.is_relative_to(ROOT):
            raise RuntimeError("Acceptance receipt must remain in the isolated candidate")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized, encoding="utf-8")
    print(serialized)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
