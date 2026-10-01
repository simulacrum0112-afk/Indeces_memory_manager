"""Local, read-only observation surface; no model or Discord access."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

from . import observer_data


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ASSETS = {"": ("observer.html", "text/html; charset=utf-8"),
          "observer.css": ("observer.css", "text/css; charset=utf-8"),
          "observer.js": ("observer.js", "text/javascript; charset=utf-8")}
GUIDE_MARKER = "# Indeces scratch directory"
# Exact v0.6.0 fixed templates are compatibility inputs, never new display text.
# Retain their complete bytes so a name migration cannot overwrite user notes.
LEGACY_GUIDE = """# Indices scratch directory

这是 Indices 的可核验运行记录目录。日期 JSONL 是原始记录；每行一条事件。
请在编辑器中只读查看，勿修改序号、hash 或 retention checkpoint。

人类可读的浏览方式：
- Console 输入 `observe`，打开所显示的本机 URL；或在另一个终端执行
  `python -m indeces observe --config <配置路径>`。`start` 也会显示观察页面 URL。
- 页面可查看 NPMI、动态边、来源版本、逐步权重变化和最近运行 trace。
- `python -m indeces scratch --config <配置路径>` 做完整运行记录合同校验。
- Console 输入 `logs` 查看本目录路径；本目录可直接用资源管理器/编辑器打开。

滚动 24 小时按每条 UTC 时间计算。服务启动和运行中每 60 秒清理到期记录；
停服期间不执行清理。观察页面只展示最近 24 小时，并不会清理原文件。
跨窗口 trace 可能不完整；checkpoint 保留前缀 hash 和截断声明，不保留到期原文。
知识版本和图事件审计在 SQLite 中按原持久策略保留，不属于 scratch 的 24 小时策略。

目录中的 index.html/README.md 只有固定使用说明，不复制私有 trace。
页面不落盘缓存或生成日志副本。认证凭据不进入 scratch。
材料召回、字面引用和语义支持是不同结果；页面不证明事实或语义蕴含。
"""
LEGACY_LANDING = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Indices · Scratch</title><body>
<h1>Indices 运行记录</h1><p>此目录的日期 JSONL 是原始可核验记录。</p>
<p>在 Console 输入 <code>observe</code>，或另开终端运行
<code>python -m indeces observe</code>，打开 Console 显示的本机 URL，
即可交互查看 NPMI、动态权重、来源版本和最近 24 小时 trace。</p>
<p><code>start</code> 也会显示观察页面 URL。
完整记录校验：<code>python -m indeces scratch</code>。</p>
<p><a href="README.md">阅读目录说明</a>。停服时页面不清理磁盘记录；图审计独立持久保存。</p>
</body></html>"""
GUIDE = LEGACY_GUIDE.replace("Indices", "Indeces")
LANDING = LEGACY_LANDING.replace("Indices", "Indeces")


def _replace_managed_guide(target: Path, previous: bytes, desired: bytes):
    """Replace only an unchanged, byte-exact known guide using a same-dir swap."""
    descriptor, name = tempfile.mkstemp(prefix=".scratch-guide-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(desired)
            output.flush()
            os.fsync(output.fileno())
        if target.is_symlink():
            raise ValueError("scratch guide must not be a link")
        # A user may edit the guide during preparation. Re-check immediately
        # before publication and preserve anything that no longer matches.
        if not target.is_file() or target.read_bytes() != previous:
            return
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_scratch_directory(directory: Path):
    """Create fixed guides or migrate exact managed templates; preserve user files."""
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("scratch directory must not be a link")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.resolve() != directory.absolute():
        raise ValueError("scratch directory must be canonical")
    for name, content, previous in (("README.md", GUIDE, LEGACY_GUIDE),
                                   ("index.html", LANDING, LEGACY_LANDING)):
        target = directory / name
        if target.is_symlink():
            raise ValueError("scratch guide must not be a link")
        try:
            with target.open("xb") as output:
                output.write(content.encode("utf-8"))
        except FileExistsError:
            desired, old = content.encode("utf-8"), previous.encode("utf-8")
            if not target.is_file() or target.stat().st_size not in {len(old), len(desired)}:
                continue
            current = target.read_bytes()
            if current == old and current != desired:
                _replace_managed_guide(target, old, desired)
            # The current managed template is already correct. Every other
            # byte sequence belongs to the user and is intentionally preserved.


class _Server(HTTPServer):
    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(2.0)
        return sock, address

    def handle_error(self, request, client_address):
        # Neither private request paths nor data/transport exceptions are logged.
        pass


class ObserverServer:
    """One serial GET server, confined to loopback and an ephemeral private URL."""
    def __init__(self, config):
        self.config = config
        self.prefix = "/" + secrets.token_urlsafe(24) + "/"
        self._server = None
        self._thread = None
        self.url = None

    def start(self):
        if self._server is not None:
            raise RuntimeError("observer already started")
        assets = {key: (files("indeces").joinpath("web", name).read_bytes(), mime)
                  for key, (name, mime) in ASSETS.items()}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            server_version = "Indeces"
            sys_version = ""

            def log_message(self, *args):
                pass

            def respond(self, status, body, content_type="application/json; charset=utf-8"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(body)
                    except (OSError, TimeoutError):
                        pass

            def error(self, status, code):
                self.respond(status, json.dumps({"error": code}).encode("utf-8"))

            def do_GET(self):
                address = f"127.0.0.1:{owner._server.server_port}"
                if self.headers.get_all("Host") != [address]:
                    return self.error(403, "host_rejected")
                origins = self.headers.get_all("Origin", [])
                if origins and origins != ["http://" + address]:
                    return self.error(403, "origin_rejected")
                sites = self.headers.get_all("Sec-Fetch-Site", [])
                if sites and sites not in (["none"], ["same-origin"]):
                    return self.error(403, "site_rejected")
                if len(self.path) > 4096:
                    return self.error(414, "request_too_large")
                try:
                    parsed = urlsplit(self.path)
                    if parsed.scheme or parsed.netloc or not parsed.path.startswith(owner.prefix):
                        return self.error(404, "not_found")
                    route = parsed.path[len(owner.prefix):]
                    if route in assets and not parsed.query:
                        body, mime = assets[route]
                        return self.respond(200, body, mime)
                    parameters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
                    expected = {"api/snapshot": (), "api/edge": ("a", "b"),
                                "api/source": ("id",), "api/trace": ("id",)}.get(route)
                    if expected is None:
                        return self.error(404, "not_found")
                    if set(parameters) != set(expected) or any(len(v) != 1 or not v[0] or len(v[0]) > 512 for v in parameters.values()):
                        return self.error(400, "invalid_parameters")
                    if route == "api/snapshot":
                        result = observer_data.snapshot(owner.config)
                    elif route == "api/edge":
                        result = observer_data.edge_details(owner.config, parameters["a"][0], parameters["b"][0])
                    elif route == "api/source":
                        result = observer_data.source_details(owner.config, parameters["id"][0])
                    else:
                        result = observer_data.trace_details(owner.config, parameters["id"][0])
                    body = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
                    if len(body) > MAX_RESPONSE_BYTES:
                        return self.error(413, "snapshot_too_large")
                    self.respond(200, body)
                except ValueError:
                    self.error(400, "invalid_request")
                except Exception:
                    self.error(503, "observation_unavailable")

            def deny(self):
                self.error(405, "read_only_get")

            do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = deny

        server = _Server(("127.0.0.1", 0), Handler)
        self._server = server
        self.url = f"http://127.0.0.1:{server.server_port}{self.prefix}"
        try:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05},
                                      name="indeces-observer", daemon=True)
            self._thread = thread
            thread.start()
        except BaseException:
            self._server = self._thread = self.url = None
            server.server_close()
            raise
        return self.url

    def close(self):
        server, thread = self._server, self._thread
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join()
        self._server = self._thread = self.url = None


def observe(config):
    prepare_scratch_directory(config.scratch_dir)
    observer = ObserverServer(config)
    try:
        print(f"Indeces read-only observer: {observer.start()}", flush=True)
        print(f"Scratch directory: {config.scratch_dir}; Ctrl+C stops this observer.", flush=True)
        # An unbounded thread-lock wait can delay Python's Ctrl+C handler on
        # Windows before 3.14. Return to Python regularly while keeping this
        # standalone observer alive until its server thread exits.
        while observer._thread.is_alive():
            observer._thread.join(timeout=0.2)
    except KeyboardInterrupt:
        print("Indeces observer stopped.", flush=True)
    finally:
        observer.close()


def show_logs(config):
    prepare_scratch_directory(config.scratch_dir)
    print(f"Scratch directory: {config.scratch_dir}")
    print(f"Directory guide: {config.scratch_dir / 'README.md'}")
    print("Open this directory in your file manager/editor, or use observe for readable traces and graph details.")
