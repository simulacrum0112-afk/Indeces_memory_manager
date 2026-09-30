"""Independent browser boundary checks using only synthetic local fixtures."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from indeces import observer
from indeces.config import load_config


class ObserverBoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name).resolve()
        base = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        config = replace(base, state_dir=root / "state", scratch_dir=root / "scratch", knowledge_dir=root / "knowledge")
        self.server = observer.ObserverServer(config)
        self.server.start()
        self.addCleanup(self.server.close)
        self.address = urlsplit(self.server.url).netloc
        self.port = urlsplit(self.server.url).port

    def request(self, route="api/snapshot", *, method="GET", headers=None, absolute=False):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            target = ("http://" + self.address if absolute else "") + self.server.prefix + route
            connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            for name, value in headers if headers is not None else [("Host", self.address)]:
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_duplicate_security_headers_and_missing_host_reject_before_private_reads(self):
        for headers in (
            [], [("Host", self.address), ("Host", self.address)],
            [("Host", self.address), ("Origin", "http://" + self.address), ("Origin", "http://" + self.address)],
            [("Host", self.address), ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")],
        ):
            with self.subTest(headers=headers), patch.object(observer.observer_data, "snapshot") as read:
                self.assertEqual(self.request(headers=headers)[0], 403)
                read.assert_not_called()

    def test_absolute_request_target_is_rejected_before_private_reads(self):
        with patch.object(observer.observer_data, "snapshot") as read:
            self.assertEqual(self.request(absolute=True)[0], 404)
        read.assert_not_called()

    def test_trace_connect_and_unknown_routes_do_not_invoke_any_data_function(self):
        with patch.object(observer.observer_data, "snapshot") as snapshot, \
                patch.object(observer.observer_data, "source_details") as source, \
                patch.object(observer.observer_data, "trace_details") as trace, \
                patch.object(observer.observer_data, "edge_details") as edge:
            for method in ("TRACE", "CONNECT"):
                self.assertEqual(self.request(method=method)[0], 405)
            for route in ("api/snapshot/../source", "api/%2e%2e/config.local.toml", "../state/memory.sqlite3"):
                self.assertEqual(self.request(route)[0], 404)
        for function in (snapshot, source, trace, edge):
            function.assert_not_called()

    def test_failures_have_no_private_transport_logs_and_server_can_continue(self):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error), \
                patch.object(observer.observer_data, "snapshot", side_effect=OSError("synthetic-private-error")):
            status, headers, body = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "observation_unavailable"})
        combined = output.getvalue() + error.getvalue() + body.decode()
        self.assertNotIn("synthetic-private-error", combined)
        self.assertNotIn(self.server.prefix, combined)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(self.request()[0], 200)

    def test_untrusted_source_text_is_json_data_with_strict_browser_headers(self):
        value = "</script><img src=x onerror=alert('synthetic')>"
        with patch.object(observer.observer_data, "snapshot", return_value={"untrusted": value}):
            status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"untrusted": value})
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-eval", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_long_request_rejected_without_reading_runtime_data(self):
        with patch.object(observer.observer_data, "snapshot") as read:
            status, _, body = self.request("api/snapshot?" + "x" * 4096)
        self.assertEqual(status, 414)
        self.assertEqual(json.loads(body), {"error": "request_too_large"})
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
