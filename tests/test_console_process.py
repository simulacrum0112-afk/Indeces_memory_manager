"""Real entrypoint/IPC process tests; the service is a synthetic await only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from indeces.console_instance import instance_directory, route_existing


SCRIPT = r"""
import asyncio,os,sys
from types import SimpleNamespace
from indeces import console
console._startup_credentials=lambda config,path:('synthetic-key','synthetic-token')
console.show_knowledge_directory=lambda config:print('TEST_KNOWLEDGE_PANEL',flush=True)
console.configure_discord=lambda path:print('TEST_DISCORD_WIZARD',flush=True) or True
console.configure_api_key=lambda path:print('TEST_APIKEY_WIZARD',flush=True) or True
async def serve(config,key,token,*,lease,knowledge_ready):
    knowledge_ready(SimpleNamespace(retry_failed=lambda path,**kw:{'queued':[],'blocked':[],'unchanged':[]}))
    print('TEST_WORKER_READY',flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        print('TEST_WORKER_CLOSED',flush=True)
console.serve=serve
print('TEST_OWNER_PID='+str(os.getpid()),flush=True)
sys.argv=['indeces','console','--config',sys.argv[1]]
console.main()
"""


class ConsoleProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(__file__).resolve().parents[1]
        self.config = Path(self.temporary.name).resolve() / "synthetic.toml"
        from indeces.discord_wizard import patch_config
        self.config.write_bytes(patch_config((self.root / "config.example.toml").read_bytes(),
                                            "123456789012345678", ()))
        self.env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        self.process = subprocess.Popen([sys.executable, "-c", SCRIPT, str(self.config)], cwd=self.root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", env=self.env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.output = []
        self._reader = threading.Thread(target=self._capture, daemon=True)
        self._reader.start()
        self.addCleanup(self.cleanup)
        self.wait(lambda: (instance_directory(self.config) / "endpoint.json").exists())

    def _capture(self):
        try:
            for line in self.process.stdout:
                self.output.append(line)
        except (ValueError, OSError):
            pass

    def wait(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("synthetic console did not respond: " + "".join(self.output)[-1000:])
            time.sleep(.02)

    def text(self):
        return "".join(self.output)

    def send(self, text):
        self.process.stdin.write(text + "\n")
        self.process.stdin.flush()

    def client(self, command, *arguments):
        return subprocess.run([sys.executable, "-m", "indeces", command,
                               "--config", str(self.config), *arguments],
            cwd=self.root, encoding="utf-8", capture_output=True, env=self.env, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def cleanup(self):
        if self.process.poll() is None:
            try:
                route_existing(self.config, "stop", wait_seconds=.5)
                self.send("quit")
                self.process.wait(timeout=5)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                # Only this synthetic process belongs to the test harness.
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stdin.close()
        self._reader.join(timeout=1)
        self.process.stdout.close()

    def test_actual_cli_continuous_and_concurrent_entries_reuse_one_console_and_panels(self):
        endpoint = instance_directory(self.config) / "endpoint.json"
        original = json.loads(endpoint.read_text())
        commands = ["console", "status", "knowledge", "npmi", "logs", "audit", "check", "service", "observe"]
        for command in commands:
            result = self.client(command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("已连接现有", result.stdout)
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(self.client, ["console", "status", "knowledge", "logs", "npmi", "service"]))
        self.assertTrue(all(result.returncode == 0 and "已连接现有" in result.stdout for result in results))
        self.wait(lambda: "TEST_KNOWLEDGE_PANEL" in self.text())
        self.assertEqual(json.loads(endpoint.read_text()), original)
        self.assertIn("TEST_OWNER_PID=" + str(original["pid"]), self.text())
        self.send("quit")
        self.process.wait(timeout=5)
        self.assertEqual(self.process.returncode, 0)
        self.assertFalse(endpoint.exists())

    def test_background_task_remote_navigation_repeated_start_stop_and_restart(self):
        self.send("start")
        self.wait(lambda: self.client("service").returncode == 0 and "服务任务状态：running" in self.text())
        first = json.loads((instance_directory(self.config) / "endpoint.json").read_text())
        for command in ["knowledge", "start", "observe", "logs", "status", "service"]:
            self.assertEqual(self.client(command).returncode, 0)
        self.send("quit")
        self.wait(lambda: "quit 不停止任务" in self.text())
        self.assertIsNone(self.process.poll())
        self.assertEqual(self.client("stop").returncode, 0)
        self.wait(lambda: "服务任务状态：stopped" in self.text())
        self.send("start")
        self.wait(lambda: self.client("service").returncode == 0 and self.text().count("服务任务状态：running") >= 2)
        self.assertEqual(json.loads((instance_directory(self.config) / "endpoint.json").read_text()), first)
        self.assertEqual(self.client("stop").returncode, 0)
        self.wait(lambda: self.text().count("服务任务状态：stopped") >= 2)
        self.send("quit")
        self.process.wait(timeout=5)
        self.assertEqual(self.process.returncode, 0)
        self.assertIn("TEST_WORKER_CLOSED", self.text())

    @unittest.skipUnless(os.name == "nt", ".cmd launchers are Windows entrypoints")
    def test_windows_cmd_launchers_route_same_owner_and_forward_config_argument(self):
        original = json.loads((instance_directory(self.config) / "endpoint.json").read_text())
        for name in ("Indeces-Console.cmd", "Indeces-Knowledge.cmd"):
            result = subprocess.run(["cmd", "/c", str(self.root / name), "--config", str(self.config)],
                                    cwd=self.root, capture_output=True, env=self.env, timeout=8,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("已连接现有".encode("utf-8"), result.stdout)
        result = subprocess.run(["cmd", "/c", str(self.root / "Indeces-Console.cmd"), "status",
                                 "--config", str(self.config)], cwd=self.root, capture_output=True,
                                env=self.env, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已连接现有".encode("utf-8"), result.stdout)
        self.assertEqual(json.loads((instance_directory(self.config) / "endpoint.json").read_text()), original)


if __name__ == "__main__":
    unittest.main()
