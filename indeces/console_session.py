"""Responsive panels and background service ownership for the existing CLI."""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import os
import queue
import select
import sys
import threading
import time

from .console_instance import ConsoleInstance, route_existing
from .credentials import CredentialError
from .contracts import GovernedError


HELP = "discord | apikey | knowledge | start | status | npmi | scratch | observe | logs | audit | retry | service | stop | home | refresh | quit"


class ServiceTask:
    """Own one event loop; navigation never replaces or restarts its task."""
    def __init__(self, config, key, token, lease, serve, *, kind="gateway"):
        self.state = "starting"
        self.error = None
        self.kind = kind
        self.result = None
        self._loop = self._task = None
        self._stop_requested = threading.Event()
        self._done = threading.Event()
        self._lease = lease
        self._knowledge = None
        self._thread = threading.Thread(target=self._run,
            args=(config, key, token, serve), name="indeces-console-service", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            lease.close()
            raise

    @property
    def active(self):
        return not self._done.is_set()

    def _run(self, config, key, token, serve):
        async def execute():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            if self._stop_requested.is_set():
                self._task.cancel()
            else:
                self.state = "running"
            return await serve(config, key, token, lease=self._lease,
                               knowledge_ready=lambda knowledge: setattr(self, "_knowledge", knowledge))
        try:
            self.result = asyncio.run(execute())
        except asyncio.CancelledError:
            self.state = "stopped"
        except BaseException as error:
            self.error = error.code if isinstance(error, (CredentialError, GovernedError)) else type(error).__name__
            self.state = "failed"
        else:
            self.state = "stopped"
        finally:
            self._loop = self._task = None
            self._lease.close()
            self._done.set()

    def stop(self):
        if not self.active or self._stop_requested.is_set():
            return
        self.state = "stopping"
        self._stop_requested.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # The service loop already finished; finalization owns cleanup.
                pass

    def retry(self, path=None):
        loop = self._loop
        if not self.active or loop is None or self._knowledge is None:
            raise RuntimeError("knowledge_service_not_ready")
        async def request():
            # SQLite and wake events remain on their original owning loop.
            return self._knowledge.retry_failed(path, include_incomplete=True)
        return asyncio.run_coroutine_threadsafe(request(), loop)


class PanelOutput:
    """Main-thread output stays visible; background output is a volatile panel."""
    def __init__(self, stream, owner, lines, lock, sinks=None):
        self.stream, self.owner, self.lines, self.lock = stream, owner, lines, lock
        self.sinks = sinks if sinks is not None else {}
        self._partial = {}
        self.total_lines = 0

    def write(self, text):
        if (threading.get_ident() == self.owner or
                threading.current_thread().name == "indeces-console-input"):
            return self.stream.write(text)
        with self.lock:
            identity = threading.get_ident()
            accumulated = self._partial.get(identity, "") + text
            parts = accumulated.split("\n")
            sink = self.sinks.get(identity)
            for line in parts[:-1]:
                if sink is None:
                    self.lines.append(line[:8192])
                    self.total_lines += 1
                else:
                    sink.lines.append(line[:8192])
                    sink.total_lines += 1
            # Provider/tool output cannot allocate an unbounded partial line.
            self._partial[identity] = parts[-1][-8192:]
        return len(text)

    def flush(self):
        # An input worker's builtin input() also flushes the visible prompt.
        # Background writes were captured already, so flushing is safe.
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class CommandReader:
    """Poll real terminals; a daemon reader is only needed for pipe/test stdin."""
    def __init__(self):
        import builtins
        native = (getattr(builtins.input, "__module__", "") == "builtins"
                  and sys.stdin is not None and sys.stdin.isatty())
        self.mode = ("windows" if os.name == "nt" else "posix") if native else "thread"
        self.prompted = False
        self.text = ""
        self._thread = None
        self._results = queue.Queue()

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def invalidate(self):
        if self.mode != "thread":
            self.prompted = False

    def before_prompt(self):
        if self.busy:
            return False
        # Drop partial command input before a wizard's hidden credential prompt.
        if self.mode == "posix":
            import termios
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        self.text = ""
        self.prompted = False
        print()
        return True

    def _read(self):
        try:
            result = input("Indeces> ")
        except (EOFError, KeyboardInterrupt, StopIteration) as error:
            result = error
        self._results.put(result)

    def poll(self):
        if self.mode == "thread":
            if self._thread is None:
                self._thread = threading.Thread(target=self._read, name="indeces-console-input", daemon=True)
                self._thread.start()
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return None
            self._thread.join()
            self._thread = None
            if isinstance(result, BaseException):
                if isinstance(result, KeyboardInterrupt):
                    raise KeyboardInterrupt
                raise EOFError
            return result
        if not self.prompted:
            print("Indeces> " + self.text, end="", flush=True)
            self.prompted = True
        if self.mode == "posix":
            readable, _, _ = select.select([sys.stdin], [], [], 0)
            if not readable:
                return None
            self.prompted = False
            return input()
        import msvcrt
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in {"\x00", "\xe0"}:
                msvcrt.getwch()  # Ignore navigation/function key suffixes.
            elif char == "\x03":
                self.text = ""
                self.prompted = False
                print("^C")
                raise KeyboardInterrupt
            elif char == "\x1a":
                raise EOFError
            elif char in {"\r", "\n"}:
                result, self.text = self.text, ""
                self.prompted = False
                print()
                return result
            elif char == "\b":
                if self.text:
                    self.text = self.text[:-1]
                    print("\b \b", end="", flush=True)
            elif char.isprintable():
                self.text += char
                print(char, end="", flush=True)
        return None


class ReadOnlyJob:
    """Slow corpus/log reads have independent lifetimes, never model slots."""
    def __init__(self, command, action, sinks, lock):
        self.command = command
        self.state = "running"
        self.error = None
        self.lines = deque(maxlen=5000)
        self.total_lines = 0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(action, sinks, lock),
                                        name="indeces-panel-" + command, daemon=True)
        self._thread.start()

    @property
    def active(self):
        return not self._done.is_set()

    def _run(self, action, sinks, lock):
        identity = threading.get_ident()
        with lock:
            sinks[identity] = self
        try:
            action()
        except BaseException as error:
            self.error = error.code if isinstance(error, (GovernedError, CredentialError)) else type(error).__name__
            self.state = "failed"
        else:
            self.state = "completed"
        finally:
            with lock:
                sinks.pop(identity, None)
            self._done.set()


class ConsoleSession:
    def __init__(self, config_path, instance):
        self.path = config_path.resolve()
        self.instance = instance
        self.reader = CommandReader()
        self.panel = "home"
        self.service = None
        self.observer = None
        self._observer_config = None
        self.lines = deque(maxlen=5000)
        self._lines_lock = threading.Lock()
        self._output_sinks = {}
        self.read_jobs = {}
        self._read_receipts = {}
        self._last_knowledge = None
        self._last_service_state = None
        self._next_refresh = 0
        self._pending = deque()
        self._retries = []
        self._stdout = self._stderr = None

    def _install_output(self):
        owner = threading.get_ident()
        self._stdout, self._stderr = sys.stdout, sys.stderr
        self.stdout = PanelOutput(sys.stdout, owner, self.lines, self._lines_lock, self._output_sinks)
        self.stderr = PanelOutput(sys.stderr, owner, self.lines, self._lines_lock, self._output_sinks)
        sys.stdout, sys.stderr = self.stdout, self.stderr
        self._handlers = []
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream is self._stderr:
                self._handlers.append((handler, handler.stream))
                handler.setStream(self.stderr)

    def close(self):
        try:
            if self.observer is not None:
                self.observer.close()
                self.observer = None
        finally:
            if self._stdout is not None:
                for handler, original in self._handlers:
                    handler.setStream(original)
                sys.stdout, sys.stderr = self._stdout, self._stderr
                self._stdout = self._stderr = None

    def _service_panel(self):
        state = self.service.state if self.service is not None else "not_started"
        kind = self.service.kind if self.service is not None else "none"
        print(f"后台任务：{kind}/{state}；任务运行不代表 Gateway 已连接或模型验收通过。")
        if self.service is not None and self.service.error:
            print(f"任务错误：{self.service.error}；完整运行记录请查看 scratch/observe。")
        with self._lines_lock:
            lines = list(self.lines)
        total = self.stdout.total_lines + self.stderr.total_lines if self._stdout is not None else len(lines)
        print(f"服务输出面板：显示最近 {min(100, len(lines))} 行；内存保留 {len(lines)} 行（上限 5000）；"
              f"本次已接收 {total} 行。完整可核验记录使用 scratch/observe。")
        if lines:
            from .knowledge_progress import _terminal_text
            for line in lines[-100:]:
                print(_terminal_text(line))
        if self.service is not None and self.service.result is not None:
            from .knowledge_progress import _terminal_text
            print("任务结果：" + _terminal_text(self.service.result))
        for name, job in self.read_jobs.items():
            print(f"只读面板任务：{name}/{job.state}" + (f"；错误={job.error}" if job.error else ""))

    def _read_panel(self, command, action=None):
        if self._stdout is None:
            # Direct programmatic/CLI users keep synchronous results.
            if action is not None:
                action()
            return
        job = self.read_jobs.get(command)
        if action is not None and (job is None or not job.active):
            job = self.read_jobs[command] = ReadOnlyJob(command, action, self._output_sinks, self._lines_lock)
        if job is None:
            return
        from .knowledge_progress import _terminal_text
        with self._lines_lock:
            lines, total = list(job.lines), job.total_lines
        print(f"只读 {command} 面板：{job.state}；菜单仍可操作；"
              f"显示最近 {min(100,len(lines))}/{total} 行，内存上限 5000 行。")
        if job.error:
            print(f"只读任务错误：{job.error}；后台服务与知识状态未修改。")
        for line in lines[-100:]:
            print(_terminal_text(line))
        if total > 100:
            print(f"完整输出可在现有终端运行 {command} --once；没有创建导出副本。")
        self._read_receipts[command] = (job.state, total)

    def _start(self, config):
        from . import console
        if self.service is not None and self.service.active:
            if self.service.kind == "knowledge_retry":
                print("当前知识维护任务仍在运行；复用任务状态，没有启动 Discord。维护完成后可显式 start。")
            else:
                print("复用现有服务任务；未重启、未建立第二个 Gateway。")
            self._service_panel()
            return
        if not config.discord.guild_id:
            raise ValueError("use discord setup to configure the Guild ID first")
        lease = console.InstanceLock(config.state_dir)
        try:
            credentials = console._startup_credentials(config, self.path)
            if credentials is None:
                lease.close()
                return
            key, token = credentials
            console._check_start_config(config, self.path)
            self.service = ServiceTask(config, key, token, lease, console.serve)
        except BaseException:
            lease.close()
            raise
        print("服务任务已交给当前 Console 的后台线程；可继续 knowledge/status/observe/logs。"
              "stop 或 Ctrl+C 请求停止；service 查看任务输出。")

    def _knowledge_panel(self, config, *, directory=False, force=False):
        from . import console
        from .knowledge_progress import progress_snapshot, _render
        if directory:
            console.show_knowledge_directory(config)
        snapshot = progress_snapshot(config)
        if force or snapshot != self._last_knowledge:
            print(_render(snapshot), flush=True)
            self._last_knowledge = snapshot

    def _retry(self, config, path):
        from . import console
        if self.service is not None and self.service.active:
            if self.service.kind == "knowledge_retry":
                print("复用运行中的知识重试任务；本次没有追加目标或重新执行任务。任务完成后可再次 retry 指定其他项。")
                self._service_panel()
                return
            self._retries.append(self.service.retry(path))
            print("已向当前后台任务请求重试失败/未完成项；结果将在当前 Console 显示。")
            return
        from .ingest import retry_ingestion
        lease = console.InstanceLock(config.state_dir)
        try:
            console._check_start_config(config, self.path)
            key = console.os.environ.get("OPENAI_API_KEY") or console.load_openai_key(self.path)
            key = console.validate_api_key(key or console.prompt_secret("OpenAI API key (session only; failed knowledge retry, no Discord): "))
            console._check_start_config(config, self.path)
            async def maintenance(config, key, unused, *, lease, knowledge_ready):
                return await retry_ingestion(config, self.path, paths=[path] if path else None,
                                             key=key, lease=lease, knowledge_ready=knowledge_ready)
            self.service = ServiceTask(config, key, None, lease, maintenance, kind="knowledge_retry")
        except (EOFError, KeyboardInterrupt):
            lease.close()
            print("重试已取消；没有启动模型请求。")
            return
        except BaseException:
            lease.close()
            raise
        print("知识重试任务在当前 Console 后台执行；未启动 Discord。knowledge/audit 查看进度，stop 请求停止。")

    def dispatch(self, command):
        from . import console
        raw = command.strip()
        verb, separator, argument = raw.partition(" ")
        command = verb.lower()
        argument = argument.strip().strip('"') if separator else None
        if argument and command != "retry":
            print("只有 retry 接收相对材料路径；其他入口不接收参数。")
            return True
        if command in {"quit", "exit"}:
            if self.service is not None and self.service.active:
                print("当前 Console 持有运行中的服务；quit 不停止任务。请先 stop，清理完成后再 quit。")
                return True
            if self.panel in self.read_jobs and not self.read_jobs[self.panel].active:
                self._read_panel(self.panel)
            if hasattr(self.instance, "begin_close"):
                self.instance.begin_close()
            return False
        if not command:
            return True
        if command in {"console", "home", "back", "help", "menu"}:
            self.panel = "home"
            print("Commands: " + HELP)
            return True
        refreshing = command == "refresh"
        if refreshing:
            command = self.panel
            if command == "home":
                print("Commands: " + HELP)
                return True
        if command not in console.CONSOLE_COMMANDS:
            print("Commands: " + HELP)
            return True
        self.panel = ("service" if command in {"start", "stop", "retry"} else
                      "home" if command in {"discord", "apikey", "init"} else command)
        # Control the task we already own even when its config was moved or
        # externally edited. Loading a new TOML is irrelevant to cancellation.
        if command == "stop":
            if self.service is not None and self.service.active:
                self.service.stop()
                print("已请求停止当前 Console 持有的服务；清理在后台进行，菜单仍可操作。")
            else:
                print("当前 Console 没有运行中的服务；未停止其他实例或进程。")
            return True
        if command == "service":
            self._service_panel()
            return True
        if command == "init":
            console.initialize(self.path)
            return True
        if command == "check":
            console.offline_check(self.path)
            return True
        if command in {"discord", "apikey"}:
            {"discord": console.configure_discord, "apikey": console.configure_api_key}[command](self.path)
            return True
        config = console.load_config(self.path)
        if command == "start":
            self._start(config)
        elif command == "knowledge":
            self._knowledge_panel(config, directory=True, force=True)
        elif command == "observe":
            if self.observer is None or self._observer_config != config:
                if self.observer is not None:
                    self.observer.close()
                    self.observer = None
                    self._observer_config = None
                console.prepare_scratch_directory(config.scratch_dir)
                candidate = console.ObserverServer(config)
                try:
                    candidate.start()
                except BaseException:
                    try:
                        candidate.close()
                    except Exception:
                        pass
                    raise
                self.observer = candidate
                self._observer_config = config
            print(f"只读观察面板：{self.observer.url}；切换页面不会重启观察或服务。")
            console.npmi(config)
        elif command == "audit":
            from .knowledge_audit import audit_snapshot, render_audit
            self._read_panel(command, None if refreshing else lambda: print(render_audit(audit_snapshot(config))))
        elif command == "scratch":
            self._read_panel(command, None if refreshing else lambda: console.check_scratch(config))
        elif command == "npmi":
            self._read_panel(command, None if refreshing else lambda: console.npmi(config))
        elif command == "retry":
            self._retry(config, argument)
        elif command == "status":
            console.status(config, self.path)
            self._service_panel()
        else:
            {"logs": console.show_logs}[command](config)
            if command == "logs":
                self._service_panel()
        return True

    def _safe_dispatch(self, command):
        try:
            return self.dispatch(command)
        except CredentialError as error:
            print(f"Credential error: {error.code}. Use discord setup for the Bot token, or apikey for the OpenAI key.")
        except Exception as error:
            print(f"Command failed: {type(error).__name__}. Check configuration and scratch records.")
        finally:
            self.reader.invalidate()
            sys.stdout.flush()
        return True

    def _refresh(self):
        from .knowledge_progress import _terminal_text
        for future in self._retries[:]:
            if future.done():
                self._retries.remove(future)
                try:
                    result = future.result()
                    print("重试结果：" + _terminal_text(result), flush=True)
                except Exception as error:
                    print(f"重试失败：{type(error).__name__}；现有已发布版本保留。", flush=True)
        now = time.monotonic()
        if now < self._next_refresh:
            return
        self._next_refresh = now + 2
        for name, job in self.read_jobs.items():
            state = (job.state, job.total_lines)
            if state != self._read_receipts.get(name):
                if self.panel == name:
                    self._read_panel(name)
                elif not job.active:
                    print(f"只读任务 {name}：{job.state}；选择 {name} 查看结果。", flush=True)
                    self._read_receipts[name] = state
                self.reader.invalidate()
        if self.panel == "knowledge":
            from . import console
            try:
                self._knowledge_panel(console.load_config(self.path))
            except Exception as error:
                print(f"知识库进度刷新失败：{type(error).__name__}；服务任务未改变。")
            self.reader.invalidate()
        if self.service is not None and self.service.state != self._last_service_state:
            self._last_service_state = self.service.state
            print(f"服务任务状态：{self.service.state}" +
                  (f"；错误类型={self.service.error}" if self.service.error else ""), flush=True)
            if not self.service.active and self.service.result is not None:
                print("任务结果：" + _terminal_text(self.service.result), flush=True)
            self.reader.invalidate()

    def run(self, initial="console"):
        from . import __version__
        self._install_output()
        print(f"Indeces {__version__} console: " + HELP)
        print("全部功能在当前 Console 切换；重复入口复用此实例。start 后菜单可继续操作；"
              "stop/Ctrl+C 停止本实例任务，quit 不会停止运行中的服务。")
        try:
            self._safe_dispatch(initial)
            while True:
                try:
                    while True:
                        try:
                            command = self.instance.commands.get_nowait()
                        except queue.Empty:
                            break
                        verb = command.split(" ", 1)[0]
                        needs_input = (verb in {"discord", "apikey"} or
                            verb in {"start", "retry"} and (self.service is None or not self.service.active))
                        if needs_input and self.reader.busy:
                            self._pending.append(command)
                            print(f"已切换到 {command}；当前管道输入未结束，请按 Enter 后继续凭据/启动操作。", flush=True)
                        else:
                            if needs_input:
                                self.reader.before_prompt()
                            self._safe_dispatch(command)
                    line = self.reader.poll()
                    if line is not None:
                        while self._pending:
                            self._safe_dispatch(self._pending.popleft())
                        if not self._safe_dispatch(line):
                            return
                    self._refresh()
                    if isinstance(self.reader, DetachedReader) and (self.service is None or not self.service.active):
                        return
                    time.sleep(.05)
                except KeyboardInterrupt:
                    if self.service is not None and self.service.active:
                        self.service.stop()
                        print("Ctrl+C 已请求停止当前服务；清理在后台继续，菜单可继续使用。")
                    else:
                        return
                except EOFError:
                    if self.service is None or not self.service.active:
                        return
                    # Do not stop a task merely because stdin detached. Keep its
                    # IPC owner alive, so a later fixed command can request stop.
                    print("输入已关闭；当前服务继续运行，可从另一入口发送 stop；没有自动重启。", flush=True)
                    self.reader = DetachedReader()
        finally:
            self.close()


class DetachedReader:
    busy = False
    def poll(self):
        return None
    def invalidate(self):
        pass
    def before_prompt(self):
        return True


def run_console(config_path, initial="console"):
    # A competing launcher can win between route_existing and acquiring lease.
    while True:
        verb, _, path = initial.partition(" ")
        receipt = route_existing(config_path, verb, path=path or None)
        if receipt is not None:
            print("已连接现有 Indeces Console；功能请求已送达。" +
                  ("已聚焦窗口。" if receipt.get("focused") else "当前终端不支持或未允许自动聚焦。"))
            return
        try:
            instance = ConsoleInstance(config_path)
            break
        except RuntimeError as error:
            if str(error) != "console_owned":
                raise
    try:
        ConsoleSession(config_path, instance).run(initial)
    finally:
        instance.close()
