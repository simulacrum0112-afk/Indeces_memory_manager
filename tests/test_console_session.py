"""Panel/task lifecycle tests with synthetic workers and no external calls."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from indeces import console, console_session
from indeces.config import load_config
from indeces.contracts import GovernedError
from indeces.lock import InstanceLock


class ConsoleSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "fixture.toml"
        from indeces.discord_wizard import patch_config
        original = Path(__file__).resolve().parents[1] / "config.example.toml"
        self.path.write_bytes(patch_config(original.read_bytes(), "123456789012345678", ()))
        self.config = load_config(self.path)
        self.session = console_session.ConsoleSession(self.path, SimpleNamespace(commands=queue.Queue()))
        self.addCleanup(self.session.close)

    def wait(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("synthetic worker did not reach expected state")
            time.sleep(.01)

    def start_fixture(self, *, failure=None):
        started = threading.Event()
        closed = threading.Event()
        owner_threads = []
        def retry(path, *, include_incomplete):
            owner_threads.append(threading.get_ident())
            self.assertTrue(include_incomplete)
            return {"queued": [{"path": path}], "blocked": [], "unchanged": []}
        async def serve(config, key, token, *, lease, knowledge_ready):
            owner_threads.append(threading.get_ident())
            knowledge_ready(SimpleNamespace(retry_failed=retry))
            started.set()
            print("synthetic worker progress")
            try:
                if failure is not None:
                    raise failure
                await asyncio.Event().wait()
            finally:
                closed.set()
        with patch.object(console, "_startup_credentials", return_value=("offline-key", "offline-token")), \
                patch.object(console, "serve", side_effect=serve) as call:
            self.session.dispatch("start")
            self.wait(started.is_set)
        return call, closed, owner_threads

    def test_background_service_navigation_repeated_start_and_stop_preserve_one_task(self):
        with redirect_stdout(io.StringIO()) as output:
            self.session._install_output()
            call, closed, threads = self.start_fixture()
            task = self.session.service
            with patch.object(console, "status"), patch.object(console, "show_knowledge_directory"), \
                    patch.object(console, "show_logs"), patch.object(console, "npmi"), \
                    patch.object(console, "check_scratch"), patch.object(console, "serve") as second_start:
                for command in ("knowledge", "status", "npmi", "scratch", "logs", "home", "start", "service", "refresh"):
                    self.assertTrue(self.session.dispatch(command))
                    self.assertIs(self.session.service, task)
                second_start.assert_not_called()
            self.assertTrue(task.active)
            with self.assertRaises(RuntimeError):
                InstanceLock(self.config.state_dir)
            self.assertTrue(self.session.dispatch("quit"))
            self.assertTrue(task.active)
            self.session.dispatch("stop")
            self.wait(lambda: not task.active)
            self.assertTrue(closed.is_set())
            self.assertFalse(self.session.dispatch("quit"))
            lease = InstanceLock(self.config.state_dir)
            lease.close()
            self.session.close()
        self.assertEqual(call.call_count, 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertIn("synthetic worker progress", output.getvalue())
        self.assertIn("quit 不停止任务", output.getvalue())

    def test_live_retry_executes_on_sqlite_owning_loop_and_preserves_path_case(self):
        with redirect_stdout(io.StringIO()):
            _, closed, threads = self.start_fixture()
            self.session.dispatch("retry 2026/My Paper.PDF")
            self.wait(lambda: self.session._retries[0].done())
            result = self.session._retries[0].result()
            self.assertEqual(result["queued"][0]["path"], "2026/My Paper.PDF")
            self.assertEqual(threads[0], threads[1])
            self.session.dispatch("stop")
            self.wait(lambda: not self.session.service.active)

    def test_worker_failure_is_retained_and_restart_is_explicit(self):
        for error, expected in ((RuntimeError("provider-private-text"), "RuntimeError"),
                                (GovernedError("knowledge_version_time_limit"), "knowledge_version_time_limit")):
            with self.subTest(error=expected), redirect_stdout(io.StringIO()) as output:
                self.start_fixture(failure=error)
                self.wait(lambda: not self.session.service.active)
                self.session.dispatch("service")
            self.assertEqual(self.session.service.error, expected)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn("provider-private-text", output.getvalue())
            self.assertEqual(self.session.service.state, "failed")

    def test_every_panel_entry_is_available_and_observer_reuses_server(self):
        observer = Mock(url="http://127.0.0.1:12345/offline/")
        with redirect_stdout(io.StringIO()), patch.object(console, "configure_discord") as discord, \
                patch.object(console, "configure_api_key") as apikey, \
                patch.object(console, "initialize") as init, patch.object(console, "offline_check") as check, \
                patch.object(console, "ObserverServer", return_value=observer) as factory, \
                patch.object(console, "prepare_scratch_directory"), patch.object(console, "npmi") as npmi, \
                patch.object(console, "show_knowledge_directory"), patch.object(console, "status"), \
                patch.object(console, "show_logs"), patch.object(console, "check_scratch"), \
                patch.object(console_session.ConsoleSession, "_retry") as retry:
            for command in ("init", "check", "discord", "apikey", "knowledge", "status", "npmi",
                            "scratch", "logs", "audit", "observe", "home", "observe", "retry", "stop", "service"):
                self.assertTrue(self.session.dispatch(command))
            self.session.close()
        discord.assert_called_once_with(self.path)
        apikey.assert_called_once_with(self.path)
        init.assert_called_once_with(self.path)
        check.assert_called_once_with(self.path)
        factory.assert_called_once_with(self.config)
        observer.start.assert_called_once()
        observer.close.assert_called_once()
        self.assertEqual(npmi.call_count, 3)
        retry.assert_called_once_with(self.config, None)

    def test_stopped_retry_runs_no_bot_and_navigation_never_expands_maintenance(self):
        started, finished = threading.Event(), threading.Event()
        targets = []
        async def maintenance(config, path, *, paths, key, lease, knowledge_ready):
            targets.append(paths)
            started.set()
            await asyncio.Event().wait()
        with redirect_stdout(io.StringIO()) as output, \
                patch.object(console, "load_openai_key", return_value="console-synthetic-api-key-offline-only"), \
                patch.dict(console.os.environ, {}, clear=True), \
                patch("indeces.ingest.retry_ingestion", side_effect=maintenance) as retry, \
                patch.object(console, "serve") as gateway, patch.object(console, "load_discord_token") as token:
            self.session.dispatch("retry MyPaper.pdf")
            self.wait(started.is_set)
            task = self.session.service
            self.session.dispatch("retry OtherPaper.pdf")
            self.assertIs(self.session.service, task)
            self.session.dispatch("stop")
            self.wait(lambda: not task.active)
        self.assertEqual(targets, [["MyPaper.pdf"]])
        self.assertEqual(retry.call_count, 1)
        gateway.assert_not_called()
        token.assert_not_called()
        self.assertIn("没有追加目标", output.getvalue())

    def test_background_output_is_bounded_and_hidden_during_secret_prompts(self):
        stream = io.StringIO()
        self.session._install_output()
        try:
            def write():
                for index in range(5100):
                    print(f"line {index}")
                print("x" * 10000, end="")
            worker = threading.Thread(target=write)
            worker.start()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(self.session.lines), 5000)
            self.assertEqual(self.session.lines[0], "line 100")
            self.assertLessEqual(len(next(iter(self.session.stdout._partial.values()))), 8192)
        finally:
            self.session.close()

    def test_slow_readonly_panel_remains_navigable_and_reuses_active_job(self):
        release = threading.Event()
        started = threading.Event()
        calls = []
        def slow(config):
            calls.append(config)
            started.set()
            print("synthetic verification started")
            release.wait(timeout=3)
            print("synthetic verification finished")
        with redirect_stdout(io.StringIO()) as output, patch.object(console, "check_scratch", side_effect=slow), \
                patch.object(console, "status"):
            self.session._install_output()
            self.session.dispatch("scratch")
            self.wait(started.is_set)
            task = self.session.read_jobs["scratch"]
            started_navigation = time.monotonic()
            self.session.dispatch("home")
            self.session.dispatch("status")
            self.session.dispatch("scratch")
            self.assertLess(time.monotonic() - started_navigation, .5)
            self.assertIs(self.session.read_jobs["scratch"], task)
            self.assertEqual(len(calls), 1)
            release.set()
            self.wait(lambda: not task.active)
            self.session.dispatch("refresh")
            self.assertIs(self.session.read_jobs["scratch"], task)
            self.assertEqual(len(calls), 1)
            self.assertFalse(self.session.lines)
            self.session.close()
        self.assertIn("synthetic verification finished", output.getvalue())
        self.assertEqual(task.state, "completed")

    def test_owned_stop_and_service_work_after_config_corruption_or_deletion(self):
        for broken in ("invalid", "missing"):
            with self.subTest(config=broken), redirect_stdout(io.StringIO()) as output:
                original = self.path.read_bytes()
                _, closed, _ = self.start_fixture()
                task = self.session.service
                if broken == "invalid":
                    self.path.write_text("[discord\nmalformed", encoding="utf-8")
                else:
                    self.path.unlink()
                with patch.object(console, "load_config", side_effect=AssertionError("owned controls must not read TOML")) as load:
                    self.assertTrue(self.session.dispatch("service"))
                    self.assertTrue(self.session.dispatch("stop"))
                    self.assertTrue(self.session.dispatch("refresh"))
                self.wait(lambda: not task.active)
                self.assertTrue(closed.is_set())
                load.assert_not_called()
                self.path.write_bytes(original)
            self.assertIn("已请求停止", output.getvalue())

    def test_failed_observer_start_is_cleaned_and_next_navigation_retries(self):
        failed = Mock(url=None)
        failed.start.side_effect = OSError("private-start-error")
        healthy = Mock(url="http://127.0.0.1:12345/recovered/")
        with redirect_stdout(io.StringIO()) as output, \
                patch.object(console, "ObserverServer", side_effect=[failed, healthy]) as factory, \
                patch.object(console, "prepare_scratch_directory"), patch.object(console, "npmi"):
            self.session._safe_dispatch("observe")
            self.assertIsNone(self.session.observer)
            self.assertIsNone(self.session._observer_config)
            failed.close.assert_called_once()
            self.session.dispatch("observe")
            self.assertIs(self.session.observer, healthy)
            self.assertEqual(self.session._observer_config, self.config)
            self.session.dispatch("home")
            self.session.dispatch("observe")
            healthy.start.assert_called_once()
            self.assertEqual(factory.call_count, 2)
            self.session.close()
        healthy.close.assert_called_once()
        self.assertIn("Command failed: OSError", output.getvalue())
        self.assertIn("recovered", output.getvalue())
        self.assertNotIn("private-start-error", output.getvalue())
        self.assertNotIn("只读观察面板：None", output.getvalue())


if __name__ == "__main__":
    unittest.main()
