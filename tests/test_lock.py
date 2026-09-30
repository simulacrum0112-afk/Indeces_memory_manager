import tempfile
from pathlib import Path
import unittest

from indeces.lock import InstanceLock


class InstanceLockTests(unittest.TestCase):
    def test_second_instance_cannot_own_the_same_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            first = InstanceLock(path)
            try:
                with self.assertRaises(RuntimeError):
                    InstanceLock(path)
                # An independent store is not erroneously blocked.
                other = InstanceLock(path / "other")
                other.close()
            finally:
                first.close()

    def test_release_allows_restart_without_deleting_lock_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            first = InstanceLock(path)
            first.close()
            second = InstanceLock(path)
            second.close()
            self.assertTrue((path / "runtime.lock").exists())
