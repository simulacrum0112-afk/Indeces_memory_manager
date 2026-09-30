from pathlib import Path
import shutil
import subprocess
import unittest


class ObserverFrontendContracts(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable for offline frontend checks")
    def test_graph_filters_history_and_retention(self):
        project = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(project / "tests" / "observer_frontend.test.cjs")],
                                cwd=project, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("contract checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
