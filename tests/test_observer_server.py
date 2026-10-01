from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

from indeces import console, observer
from indeces.config import load_config


class ObserverServerTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name).resolve()
        base = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.config = replace(base, state_dir=self.root / "state", scratch_dir=self.root / "scratch",
                              knowledge_dir=self.root / "knowledge")
        self.server = observer.ObserverServer(self.config)
        self.url = self.server.start()
        self.addCleanup(self.server.close)
        self.port = urlsplit(self.url).port

    def request(self, route="", *, method="GET", headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, self.server.prefix + route, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_local_random_url_assets_have_csp_and_no_cache(self):
        self.assertEqual(self.server._server.server_address[0], "127.0.0.1")
        other = observer.ObserverServer(self.config)
        self.assertNotEqual(self.server.prefix, other.prefix)
        for route, mime in (("", "text/html"), ("observer.css", "text/css"), ("observer.js", "text/javascript")):
            status, headers, body = self.request(route)
            self.assertEqual(status, 200)
            self.assertIn(mime, headers["Content-Type"])
            self.assertIn("no-store", headers["Cache-Control"])
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertTrue(body)
            self.assertEqual(headers["Server"], "Indeces ")
            if route == "":
                self.assertIn(b"<title>Indeces", body)

    def test_no_database_no_runtime_empty_snapshot_and_no_model_calls(self):
        status, _, body = self.request("api/snapshot")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["graph"]["nodes"], [])
        self.assertFalse(self.config.state_dir.exists())
        self.assertFalse(self.config.scratch_dir.exists())

    def test_each_route_passes_only_named_arguments_to_readonly_functions(self):
        for route, name, values in (("api/snapshot", "snapshot", ()),
                                    ("api/edge?a=alpha&b=beta", "edge_details", ("alpha", "beta")),
                                    ("api/source?id=kb%3Asynthetic", "source_details", ("kb:synthetic",)),
                                    ("api/trace?id=synthetic", "trace_details", ("synthetic",))):
            with self.subTest(route=route), patch.object(observer.observer_data, name, return_value={"ok": True}) as call:
                self.assertEqual(self.request(route)[0], 200)
                call.assert_called_once_with(self.config, *values)

    def test_rebinding_cross_origin_and_fetch_site_rejected_before_private_reads(self):
        for headers in ({"Host": "attacker.invalid"}, {"Origin": "http://attacker.invalid"},
                        {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers), patch.object(observer.observer_data, "snapshot") as read:
                self.assertEqual(self.request("api/snapshot", headers=headers)[0], 403)
                read.assert_not_called()

    def test_same_origin_browser_headers_accepted(self):
        address = "http://127.0.0.1:" + str(self.port)
        self.assertEqual(self.request("api/snapshot", headers={"Origin": address, "Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_wrong_secret_and_arbitrary_files_never_exposed(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("GET", "/api/snapshot")
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        connection.close()
        for route in ("../config.local.toml", "config.local.discord.secret", "api/../README.md", "%2e%2e/state/memory.sqlite3", "api/snapshot?file=secret"):
            self.assertIn(self.request(route)[0], (400, 404))

    def test_post_and_other_mutating_methods_rejected(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            with self.subTest(method=method), patch.object(observer.observer_data, "snapshot") as read:
                self.assertEqual(self.request("api/snapshot", method=method)[0], 405)
                read.assert_not_called()

    def test_duplicate_extra_and_oversized_arguments_rejected(self):
        for route in ("api/edge?a=x&a=y&b=z", "api/edge?a=x", "api/source?id=", "api/source?id=x&extra=y", "api/trace?id=" + "x" * 513, "api/snapshot?broken"):
            self.assertEqual(self.request(route)[0], 400)

    def test_errors_do_not_contain_private_text_or_secret_url(self):
        with patch.object(observer.observer_data, "snapshot", side_effect=OSError("synthetic-private-secret")):
            status, _, body = self.request("api/snapshot")
        self.assertEqual(status, 503)
        self.assertNotIn(b"synthetic-private-secret", body)
        self.assertNotIn(self.server.prefix.encode(), body)

    def test_payload_limit_and_nonfinite_outputs_not_sent(self):
        with patch.object(observer, "MAX_RESPONSE_BYTES", 4), patch.object(observer.observer_data, "snapshot", return_value={"synthetic": "large"}):
            self.assertEqual(self.request("api/snapshot")[0], 413)
        with patch.object(observer.observer_data, "snapshot", return_value={"x": float("nan")}):
            self.assertEqual(self.request("api/snapshot")[0], 400)

    def test_server_can_close_idempotently_and_releases_port(self):
        self.server.close()
        self.server.close()
        self.assertIsNone(self.server._thread)
        with self.assertRaises(OSError):
            connection = HTTPConnection("127.0.0.1", self.port, timeout=.2)
            connection.request("GET", "/")

    def test_directory_guides_contain_no_private_data_and_preserve_existing_files(self):
        directory = self.config.scratch_dir
        observer.prepare_scratch_directory(directory)
        self.assertEqual({p.name for p in directory.iterdir()}, {"README.md", "index.html"})
        self.assertIn("24", (directory / "README.md").read_text(encoding="utf-8"))
        (directory / "README.md").write_text("user-owned-synthetic-note", encoding="utf-8")
        (directory / "2026-01-01.jsonl").write_bytes(b"synthetic-raw-record\n")
        observer.prepare_scratch_directory(directory)
        self.assertEqual((directory / "README.md").read_text(encoding="utf-8"), "user-owned-synthetic-note")
        self.assertEqual((directory / "2026-01-01.jsonl").read_bytes(), b"synthetic-raw-record\n")


class ObserverConsoleTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "config.example.toml"

    def test_commands_never_request_credentials_or_start_gateway(self):
        for command, action in (("observe", "observe"), ("logs", "show_logs")):
            with self.subTest(command=command), patch("sys.argv", ["indeces", command, "--config", str(self.path)]), \
                    patch.object(console, action) as call, patch.object(console, "run") as run, \
                    patch.object(console, "prompt_secret") as prompt:
                console.main()
                call.assert_called_once()
                run.assert_not_called()
                prompt.assert_not_called()

    def test_interactive_observe_and_logs_return_to_console(self):
        with patch("sys.argv", ["indeces", "console", "--config", str(self.path)]), \
                patch("builtins.input", side_effect=["observe", "logs", "quit"]), \
                patch.object(console, "observe") as observe, patch.object(console, "show_logs") as logs, \
                patch.object(console, "open_command_window") as window, \
                redirect_stdout(io.StringIO()):
            console.main()
        window.assert_called_once_with("observe", self.path)
        observe.assert_not_called()
        logs.assert_called_once()


class ObserverGuideMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve() / "scratch"
        self.directory.mkdir()

    def test_exact_previous_managed_templates_migrate_with_atomic_replace(self):
        for name, content in (("README.md", observer.LEGACY_GUIDE), ("index.html", observer.LEGACY_LANDING)):
            (self.directory / name).write_bytes(content.encode("utf-8"))
        raw = self.directory / "2026-01-01.jsonl"
        raw.write_bytes(b"synthetic-immutable-history\n")
        with patch.object(observer.os, "replace", wraps=observer.os.replace) as replace_guide:
            observer.prepare_scratch_directory(self.directory)
        self.assertEqual(replace_guide.call_count, 2)
        self.assertEqual((self.directory / "README.md").read_bytes(), observer.GUIDE.encode("utf-8"))
        self.assertEqual((self.directory / "index.html").read_bytes(), observer.LANDING.encode("utf-8"))
        self.assertEqual(raw.read_bytes(), b"synthetic-immutable-history\n")
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"README.md", "index.html", raw.name})

    def test_current_managed_guides_are_idempotent(self):
        observer.prepare_scratch_directory(self.directory)
        before = {path.name: path.stat().st_mtime_ns for path in self.directory.iterdir()}
        with patch.object(observer.os, "replace") as replace_guide:
            observer.prepare_scratch_directory(self.directory)
        replace_guide.assert_not_called()
        self.assertEqual(before, {path.name: path.stat().st_mtime_ns for path in self.directory.iterdir()})

    def test_modified_previous_guides_and_user_files_are_preserved(self):
        custom = {"README.md": observer.LEGACY_GUIDE.replace("勿修改", "勿删除").encode("utf-8"),
                  "index.html": observer.LEGACY_LANDING.replace("Scratch", "Archive").encode("utf-8")}
        self.assertEqual(len(custom["README.md"]), len(observer.LEGACY_GUIDE.encode("utf-8")))
        self.assertEqual(len(custom["index.html"]), len(observer.LEGACY_LANDING.encode("utf-8")))
        for name, content in custom.items():
            (self.directory / name).write_bytes(content)
        observer.prepare_scratch_directory(self.directory)
        for name, content in custom.items():
            self.assertEqual((self.directory / name).read_bytes(), content)

    def test_user_edit_during_preparation_is_not_overwritten(self):
        target = self.directory / "README.md"
        target.write_bytes(observer.LEGACY_GUIDE.encode("utf-8"))
        with patch.object(observer.os, "fsync", side_effect=lambda _: target.write_bytes(b"user-edited-during-preparation")), \
                patch.object(observer.os, "replace") as replace_guide:
            observer.prepare_scratch_directory(self.directory)
        replace_guide.assert_not_called()
        self.assertEqual(target.read_bytes(), b"user-edited-during-preparation")
        self.assertFalse(list(self.directory.glob(".scratch-guide-*.tmp")))

    def test_failed_swap_preserves_old_guide_and_removes_temporary_file(self):
        target = self.directory / "README.md"
        old = observer.LEGACY_GUIDE.encode("utf-8")
        target.write_bytes(old)
        with patch.object(observer.os, "replace", side_effect=OSError("synthetic-swap-failure")), self.assertRaises(OSError):
            observer.prepare_scratch_directory(self.directory)
        self.assertEqual(target.read_bytes(), old)
        self.assertFalse(list(self.directory.glob(".scratch-guide-*.tmp")))


if __name__ == "__main__":
    unittest.main()
