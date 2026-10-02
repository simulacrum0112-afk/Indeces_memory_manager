"""Observer CLI wait/cleanup fixtures; no sockets, runtime data or models."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from indeces import observer


class FakeThread:
    def __init__(self, *, stop_after=2, wait_error=None):
        self.stop_after = stop_after
        self.wait_error = wait_error
        self.timeouts = []

    def is_alive(self):
        return len(self.timeouts) < self.stop_after

    def join(self, timeout=None):
        self.timeouts.append(timeout)
        # Fail immediately instead of hanging if the CLI regresses to an
        # unbounded wait. A short finite wait is the responsiveness contract.
        if timeout is None or not 0 < timeout <= 0.25:
            raise AssertionError("observer wait must have a short timeout")
        if self.wait_error is not None:
            raise self.wait_error


class ObserverWaitTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(scratch_dir=Path("synthetic-scratch-no-io"),
                                      state_dir=Path("synthetic-state-no-io"),
                                      knowledge_dir=Path("synthetic-project-no-io/knowledge"))

    def observe(self, thread, *, start_error=None):
        server = SimpleNamespace(
            _thread=thread,
            start=Mock(return_value="http://127.0.0.1:12345/synthetic-observer/",
                       side_effect=start_error),
            close=Mock())
        output = io.StringIO()
        prepare = patch.object(observer, "prepare_scratch_directory")
        create = patch.object(observer, "ObserverServer", return_value=server)
        return server, output, prepare, create

    def test_short_waits_keep_observer_alive_until_thread_exits_and_close_it(self):
        thread = FakeThread(stop_after=3)
        server, output, prepare, create = self.observe(thread)
        with prepare as guide, create as factory, redirect_stdout(output):
            observer.observe(self.config)
        self.assertEqual(thread.timeouts, [0.2, 0.2, 0.2])
        guide.assert_called_once_with(self.config.scratch_dir)
        factory.assert_called_once_with(self.config)
        server.start.assert_called_once_with()
        server.close.assert_called_once_with()
        self.assertIn("http://127.0.0.1:12345/synthetic-observer/", output.getvalue())
        self.assertNotIn("observer stopped", output.getvalue())

    def test_already_exited_thread_returns_without_wait_and_still_closes(self):
        thread = FakeThread(stop_after=0)
        server, output, prepare, create = self.observe(thread)
        with prepare, create, redirect_stdout(output):
            observer.observe(self.config)
        self.assertEqual(thread.timeouts, [])
        server.close.assert_called_once_with()

    def test_ctrl_c_during_short_wait_returns_and_closes_observer(self):
        thread = FakeThread(wait_error=KeyboardInterrupt())
        server, output, prepare, create = self.observe(thread)
        with prepare, create, redirect_stdout(output):
            observer.observe(self.config)
        self.assertEqual(thread.timeouts, [0.2])
        self.assertIn("Indeces observer stopped.", output.getvalue())
        server.close.assert_called_once_with()

    def test_wait_failure_propagates_after_close_without_claiming_ctrl_c_exit(self):
        error = RuntimeError("synthetic wait failure")
        thread = FakeThread(wait_error=error)
        server, output, prepare, create = self.observe(thread)
        with prepare, create, redirect_stdout(output), self.assertRaises(RuntimeError) as caught:
            observer.observe(self.config)
        self.assertIs(caught.exception, error)
        self.assertEqual(thread.timeouts, [0.2])
        server.close.assert_called_once_with()
        self.assertNotIn("observer stopped", output.getvalue())

    def test_start_failure_closes_without_waiting(self):
        error = OSError("synthetic observer start failure")
        thread = FakeThread()
        server, output, prepare, create = self.observe(thread, start_error=error)
        with prepare, create, redirect_stdout(output), self.assertRaises(OSError) as caught:
            observer.observe(self.config)
        self.assertIs(caught.exception, error)
        self.assertEqual(thread.timeouts, [])
        server.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
