"""One local console per canonical configuration, with bounded command routing.

The kernel lease, not a PID or the endpoint file, determines ownership. Process
death releases the lease; the next owner replaces stale rendezvous metadata.
This is a private, fixed-command socket protocol, not a general command API.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import tempfile
import threading
import time


COMMANDS = frozenset({"console", "init", "discord", "apikey", "knowledge", "start",
                      "status", "npmi", "check", "scratch", "observe", "logs", "audit",
                      "retry", "service", "stop"})
MAX_MESSAGE = 2048


def instance_directory(config_path):
    canonical = os.path.normcase(str(Path(config_path).resolve()))
    user = os.environ.get("USERNAME") or os.environ.get("USER") or str(getattr(os, "getuid", lambda: 0)())
    user_digest = hashlib.sha256(user.encode("utf-8")).hexdigest()[:16]
    config_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / ("indeces-console-" + user_digest) / config_digest


class ConsoleLease:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)
        self.stream = (directory / "console.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(self.stream.fileno()).st_size == 0:
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError("console_owned") from None

    def close(self):
        self.stream.close()


def _window_handle():
    if os.name != "nt":
        return 0
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.GetConsoleWindow.restype = ctypes.c_void_p
        return int(kernel.GetConsoleWindow() or 0)
    except (AttributeError, OSError, ValueError):
        return 0


def focus_window(handle):
    """Best effort only: some terminals are hosted or disallow focus stealing."""
    if os.name != "nt" or not handle:
        return False
    try:
        import ctypes
        user = ctypes.windll.user32
        window = ctypes.c_void_p(handle)
        if not user.IsWindow(window):
            return False
        # A pseudoconsole has a message-only HWND, never a visible window.
        if not user.IsWindowVisible(window):
            return False
        user.ShowWindow(window, 9)  # SW_RESTORE; do not close or terminate anything.
        return bool(user.SetForegroundWindow(window))
    except (AttributeError, OSError, ValueError):
        return False


def _receive(stream):
    data = bytearray()
    while len(data) <= MAX_MESSAGE:
        part = stream.recv(min(512, MAX_MESSAGE + 1 - len(data)))
        if not part:
            break
        data.extend(part)
        if b"\n" in data:
            line, _, rest = data.partition(b"\n")
            if rest or len(line) > MAX_MESSAGE:
                raise ValueError("invalid_message")
            return json.loads(line)
    raise ValueError("invalid_message")


def _send(stream, value):
    stream.sendall(json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n")


class ConsoleInstance:
    """The owner queues actions; clients receive a receipt after enqueueing."""
    def __init__(self, config_path, *, directory=None):
        self.directory = Path(directory) if directory is not None else instance_directory(config_path)
        self.path = self.directory / "endpoint.json"
        self.lease = ConsoleLease(self.directory)
        self.commands = queue.Queue(maxsize=64)
        self.token = secrets.token_urlsafe(32)
        self.window = _window_handle()
        self._closed = False
        self._closing = threading.Event()
        self._thread = self._server = None
        try:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # No SO_REUSEADDR: no competing listener may claim this endpoint.
            self._server.bind(("127.0.0.1", 0))
            self._server.listen(8)
            self._server.settimeout(.1)
            metadata = {"version": 1, "port": self._server.getsockname()[1],
                        "token": self.token, "pid": os.getpid()}
            temporary = self.directory / ("endpoint-" + secrets.token_hex(8) + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as output:
                    if os.name != "nt":
                        temporary.chmod(0o600)
                    json.dump(metadata, output)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self._thread = threading.Thread(target=self._listen, name="indeces-console-inbox", daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def _listen(self):
        while not self._closing.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                client.settimeout(.5)
                try:
                    request = _receive(client)
                    if (self._closing.is_set() or not isinstance(request, dict)
                            or set(request) not in ({"token", "command"}, {"token", "command", "path"})
                            or not isinstance(request["token"], str)
                            or not secrets.compare_digest(request["token"], self.token)
                            or request["command"] not in COMMANDS):
                        _send(client, {"accepted": False})
                        continue
                    path = request.get("path")
                    if path is not None and (request["command"] != "retry" or not isinstance(path, str)
                            or not 0 < len(path) <= 1024 or any(ord(char) < 32 for char in path)):
                        _send(client, {"accepted": False})
                        continue
                    self.commands.put_nowait(request["command"] + (" " + path if path else ""))
                    _send(client, {"accepted": True, "focused": focus_window(self.window)})
                except (OSError, ValueError, TypeError, KeyError, queue.Full):
                    # Authentication/transport errors contain no private text.
                    try:
                        _send(client, {"accepted": False})
                    except OSError:
                        pass

    def begin_close(self):
        self._closing.set()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.begin_close()
        if self._server is not None:
            self._server.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        # The lease still excludes every replacement owner while we unlink.
        self.path.unlink(missing_ok=True)
        self.lease.close()


def route_existing(config_path, command, *, path=None, directory=None, wait_seconds=3.0):
    """Return a receipt or None if no live owner; never replace a live owner."""
    if command not in COMMANDS:
        raise ValueError("unsupported_console_command")
    folder = Path(directory) if directory is not None else instance_directory(config_path)
    if not folder.exists():
        return None
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            lease = ConsoleLease(folder)
        except RuntimeError:
            pass
        else:
            lease.close()
            return None
        try:
            with (folder / "endpoint.json").open("r", encoding="utf-8") as source:
                raw = source.read(MAX_MESSAGE + 1)
            if len(raw) > MAX_MESSAGE:
                raise ValueError("invalid_endpoint")
            endpoint = json.loads(raw)
            if (not isinstance(endpoint, dict) or endpoint.get("version") != 1
                    or type(endpoint.get("port")) is not int or not 0 < endpoint["port"] < 65536
                    or not isinstance(endpoint.get("token"), str) or len(endpoint["token"]) > 128):
                raise ValueError("invalid_endpoint")
            with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=.5) as client:
                request = {"token": endpoint["token"], "command": command}
                if path is not None:
                    request["path"] = path
                _send(client, request)
                receipt = _receive(client)
            if isinstance(receipt, dict) and receipt.get("accepted") is True:
                return receipt
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("existing_console_unavailable")
        time.sleep(.05)
