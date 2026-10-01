"""Kernel leases and actual loopback IPC; all configs/data are synthetic."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from indeces.console_instance import (COMMANDS, ConsoleInstance, ConsoleLease,
                                     _receive, _send, instance_directory, route_existing)


class ConsoleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "synthetic.toml"
        self.directory = self.root / "session"

    def owner(self):
        owner = ConsoleInstance(self.path, directory=self.directory)
        self.addCleanup(owner.close)
        return owner

    def test_repeated_fixed_commands_route_to_one_owner_without_losing_order(self):
        owner = self.owner()
        for command in sorted(COMMANDS):
            receipt = route_existing(self.path, command, directory=self.directory)
            self.assertTrue(receipt["accepted"])
            self.assertEqual(owner.commands.get(timeout=1), command)
        self.assertFalse((self.root / "memory.sqlite3").exists())

    def test_simultaneous_launch_competition_has_one_winner_and_reuses_it(self):
        barrier = threading.Barrier(8)
        owners = []
        owners_lock = threading.Lock()
        def compete(index):
            barrier.wait()
            try:
                owner = ConsoleInstance(self.path, directory=self.directory)
            except RuntimeError:
                return route_existing(self.path, "knowledge", directory=self.directory)
            with owners_lock:
                owners.append(owner)
            return "owner"
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(compete, range(8)))
            self.assertEqual(results.count("owner"), 1)
            self.assertEqual(len(owners), 1)
            self.assertTrue(all(result == "owner" or result.get("accepted") for result in results))
            self.assertEqual(owners[0].commands.qsize(), 7)
        finally:
            for owner in owners:
                owner.close()

    def test_normal_close_removes_endpoint_and_immediate_restart_works(self):
        owner = ConsoleInstance(self.path, directory=self.directory)
        first = owner.token
        owner.close()
        self.assertFalse((self.directory / "endpoint.json").exists())
        self.assertIsNone(route_existing(self.path, "console", directory=self.directory))
        replacement = self.owner()
        self.assertNotEqual(first, replacement.token)

    def test_stale_endpoint_cannot_keep_dead_owner_locked(self):
        self.directory.mkdir()
        (self.directory / "endpoint.json").write_text('{"version":1,"port":1,"token":"stale","pid":1}')
        self.assertIsNone(route_existing(self.path, "console", directory=self.directory))
        owner = self.owner()
        self.assertEqual(json.loads(owner.path.read_text())["token"], owner.token)

    def test_missing_endpoint_of_live_owner_never_creates_second_instance(self):
        lease = ConsoleLease(self.directory)
        try:
            with self.assertRaisesRegex(RuntimeError, "existing_console_unavailable"):
                route_existing(self.path, "console", directory=self.directory, wait_seconds=.1)
            with self.assertRaisesRegex(RuntimeError, "console_owned"):
                ConsoleInstance(self.path, directory=self.directory)
        finally:
            lease.close()

    def test_authentication_invalid_and_general_commands_are_rejected(self):
        owner = self.owner()
        endpoint = json.loads(owner.path.read_text())
        requests = [{"token": "invalid", "command": "start"},
                    {"token": owner.token, "command": "python -c anything"},
                    {"token": owner.token, "command": ["start"]},
                    {"token": owner.token, "command": "status", "path": "anything"},
                    {"token": owner.token, "command": "retry", "path": "bad\npath"}]
        for request in requests:
            with self.subTest(request=request), socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=1) as client:
                _send(client, request)
                self.assertFalse(_receive(client)["accepted"])
        self.assertTrue(owner.commands.empty())

    def test_retry_path_is_bounded_and_case_preserving(self):
        owner = self.owner()
        receipt = route_existing(self.path, "retry", path="2026/My Paper.PDF", directory=self.directory)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(owner.commands.get(timeout=1), "retry 2026/My Paper.PDF")

    def test_config_aliases_share_identity_and_other_configs_do_not(self):
        parent = self.root / "alias"
        parent.mkdir()
        alias = parent / ".." / self.path.name
        self.assertEqual(instance_directory(self.path), instance_directory(alias))
        self.assertNotEqual(instance_directory(self.path), instance_directory(self.path.with_name("other.toml")))

    def test_abnormal_process_exit_releases_kernel_lease_and_stale_metadata(self):
        script = """import os,sys
from pathlib import Path
from indeces.console_instance import ConsoleInstance
owner=ConsoleInstance(Path(sys.argv[1]), directory=Path(sys.argv[2]))
print('ready',flush=True)
# Crash the interpreter that owns the lease, without finally/close cleanup.
# On Windows the venv executable is a launcher: killing/waiting that shim
# does not establish that its runtime child has finished releasing resources.
sys.stdin.readline()
os._exit(17)
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(self.path), str(self.directory)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertTrue(route_existing(self.path, "status", directory=self.directory)["accepted"])
            child.stdin.write("crash\n")
            child.stdin.flush()
            self.assertEqual(child.wait(timeout=5), 17)
            self.assertTrue((self.directory / "endpoint.json").exists())
            replacement = ConsoleInstance(self.path, directory=self.directory)
            replacement.close()
            self.assertFalse((self.directory / "endpoint.json").exists())
        finally:
            if child.poll() is None:
                # Closing the test-owned pipe also wakes the real owner.
                child.stdin.close()
                child.wait(timeout=5)
            if not child.stdin.closed:
                child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_route_recovers_if_killed_launcher_runtime_is_still_exiting(self):
        script = """import sys,time
from pathlib import Path
from indeces.console_instance import ConsoleInstance
owner=ConsoleInstance(Path(sys.argv[1]), directory=Path(sys.argv[2]))
print('ready',flush=True)
time.sleep(1)
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(self.path), str(self.directory)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertTrue(route_existing(self.path, "status", directory=self.directory)["accepted"])
            # Terminate only this synthetic launcher. On Windows its real owner
            # can still be exiting after wait() returns. The application routes
            # first and respects the live kernel lease until it is available.
            child.kill()
            child.wait(timeout=5)
            self.assertIsNone(route_existing(self.path, "console", directory=self.directory, wait_seconds=3))
            replacement = ConsoleInstance(self.path, directory=self.directory)
            replacement.close()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()


if __name__ == "__main__":
    unittest.main()
