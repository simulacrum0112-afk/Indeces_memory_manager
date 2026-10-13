"""Narrow Console model input-ownership regressions; all input is synthetic.

The busy case uses the actual CommandReader and builtin input on an OS pipe.
No real terminal, socket, provider, credential or model-config write occurs.
Root owns RED/GREEN execution; source changes are kept separate from this file.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import queue
import threading
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces import console, console_session


ROOT = Path(__file__).resolve().parents[1]


class ModelIPCInputOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "config.local.toml"
        self.path.write_bytes((ROOT / "config.example.toml").read_bytes())

    def run_actual_pipe(self, *, ipc):
        read_fd, write_fd = os.pipe()
        read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
        session = None
        observations = {"wizard": [], "first_tick": None}
        original_sleep = time.sleep
        started = time.monotonic()
        try:
            with patch("sys.stdin", read_stream), redirect_stdout(io.StringIO()):
                session = console_session.ConsoleSession(self.path, SimpleNamespace(commands=queue.Queue()))
                self.assertIs(type(session.reader), console_session.CommandReader)
                self.assertEqual(session.reader.mode, "thread")
                if ipc:
                    # Start the real input worker while the pipe has no bytes.
                    entered = threading.Event()
                    actual_read = session.reader._read
                    def observed_read():
                        entered.set()
                        actual_read()
                    with patch.object(session.reader, "_read", side_effect=observed_read):
                        self.assertIsNone(session.reader.poll())
                        self.assertTrue(entered.wait(2))
                        self.assertTrue(session.reader.busy)
                    session.instance.commands.put("model")
                else:
                    os.write(write_fd, b"model\nquit\n")

                def wizard(path):
                    observations["wizard"].append({"path": path, "busy": session.reader.busy,
                                                   "owner": threading.get_ident()})
                    return True

                def tick(seconds):
                    if observations["first_tick"] is None:
                        observations["first_tick"] = {
                            "busy": session.reader.busy,
                            "pending": list(session._pending),
                            "wizard_count": len(observations["wizard"]),
                        }
                        if ipc:
                            # This is the first point after IPC processing.
                            # Release the input owner, then quit through the pipe.
                            os.write(write_fd, b"\nquit\n")
                    if time.monotonic() - started > 3:
                        raise AssertionError("synthetic pipe Console did not finish")
                    original_sleep(0.001)

                with patch.object(console, "configure_model", side_effect=wizard) as model, \
                        patch.object(session, "_refresh"), \
                        patch.object(console_session.time, "sleep", side_effect=tick):
                    session.run("console")
                observations["calls"] = model.call_count
                observations["reader_busy_after"] = session.reader.busy
                return observations
        finally:
            # Every failing RED assertion also releases and joins the real input
            # worker before restoring stdin or closing its pipe.
            if session is not None:
                if session.reader.busy:
                    os.write(write_fd, b"\nquit\n")
                    session.reader._thread.join(2)
                session.close()
            read_stream.close()
            os.close(write_fd)

    def test_ipc_model_defers_while_real_pipe_reader_busy_then_runs_once_after_return(self):
        result = self.run_actual_pipe(ipc=True)
        self.assertEqual(result["first_tick"], {"busy": True, "pending": ["model"], "wizard_count": 0})
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["wizard"], [{"path": self.path, "busy": False, "owner": threading.get_ident()}])
        self.assertFalse(result["reader_busy_after"])

    def test_command_line_model_still_works_with_actual_pipe_input(self):
        result = self.run_actual_pipe(ipc=False)
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["wizard"], [{"path": self.path, "busy": False, "owner": threading.get_ident()}])
        self.assertFalse(result["reader_busy_after"])

    def test_native_reader_before_prompt_clears_partial_input_before_ipc_model(self):
        session = console_session.ConsoleSession(self.path, SimpleNamespace(commands=queue.Queue()))
        session.instance.commands.put("model")
        # Exercise the real Windows-native before_prompt implementation without
        # reading the user's keyboard. Only command completion is synthetic.
        session.reader.mode = "windows"
        session.reader.text = "synthetic partial command"
        session.reader.prompted = True
        seen = []
        def wizard(path):
            seen.append((session.reader.text, session.reader.prompted, session.reader.busy))
            return True
        try:
            with patch.object(console, "configure_model", side_effect=wizard) as model, \
                    patch.object(session.reader, "before_prompt", wraps=session.reader.before_prompt) as before, \
                    patch.object(session.reader, "poll", return_value="quit"), \
                    patch.object(session, "_refresh"), redirect_stdout(io.StringIO()):
                session.run("console")
            before.assert_called_once_with()
            model.assert_called_once_with(self.path)
            self.assertEqual(seen, [("", False, False)])
        finally:
            session.close()

    def test_ipc_model_still_refuses_setup_for_owned_active_service(self):
        session = console_session.ConsoleSession(self.path, SimpleNamespace(commands=queue.Queue()))
        service = SimpleNamespace(active=True)
        session.service = service
        session.instance.commands.put("model")
        session.reader.mode = "windows"
        def finish_after_ipc():
            service.active = False
            return "quit"
        try:
            with patch.object(console, "configure_model") as model, \
                    patch.object(console, "load_config") as loader, \
                    patch.object(session.reader, "poll", side_effect=finish_after_ipc), \
                    patch.object(session, "_refresh"), redirect_stdout(io.StringIO()):
                session.run("console")
            model.assert_not_called()
            loader.assert_not_called()
        finally:
            session.service = None
            session.close()


if __name__ == "__main__":
    unittest.main()
