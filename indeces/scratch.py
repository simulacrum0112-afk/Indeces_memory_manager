"""Request/response evidence with rolling 24-hour retention.

These are observable payloads and receipts, never hidden chain-of-thought.
The local chain is not an externally anchored proof against deliberate rewriting.
Retention removes expired prefixes without rewriting the retained evidence rows.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import uuid


_ZERO_HASH = "0" * 64
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")
_RECORD_KEYS = {"version", "sequence", "timestamp", "event", "fields", "previous_hash", "hash"}
RETENTION_SECONDS = 24 * 60 * 60
_MANAGED_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.jsonl\Z")
_TEMP_NAME = re.compile(r"\.scratch-retention-[0-9a-f]{32}\.tmp\Z")
_CHECKPOINT_KIND = "scratch_retention_checkpoint"
_CHECKPOINT_KEYS = {"kind", "version", "removed_through_sequence", "removed_head_hash", "pruned_at", "cutoff",
                    "partial_trace_ids", "partial_call_ids", "cleanup_pending", "hash"}
_READER_WAIT_MILLISECONDS = 2000
_WRITER_WAIT_MILLISECONDS = 10000


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("nonfinite JSON number")


def _decode(line):
    return json.loads(line, object_pairs_hook=_unique_object, parse_constant=_nonfinite)


def _utc_timestamp(timestamp):
    if not isinstance(timestamp, str) or not _UTC_TIME.fullmatch(timestamp):
        raise ValueError("invalid UTC timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        raise ValueError("invalid UTC timestamp") from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("invalid UTC timestamp")
    return parsed


def _validate(item, sequence, *, includes_hash):
    keys = _RECORD_KEYS if includes_hash else _RECORD_KEYS - {"hash"}
    if not isinstance(item, dict) or set(item) != keys:
        raise ValueError("invalid record fields")
    if type(item["version"]) is not int or item["version"] != 1:
        raise ValueError("invalid record version")
    if type(item["sequence"]) is not int or item["sequence"] != sequence:
        raise ValueError("invalid record sequence")
    _utc_timestamp(item["timestamp"])
    event = item["event"]
    if (not isinstance(event, str) or not event or event != event.strip() or not event.isprintable()):
        raise ValueError("invalid record event")
    if not isinstance(item["fields"], dict):
        raise ValueError("invalid event fields")
    for key in ("previous_hash", "hash") if includes_hash else ("previous_hash",):
        if not isinstance(item[key], str) or not _HASH.fullmatch(item[key]):
            raise ValueError("invalid record hash")


def _validate_checkpoint(item):
    if not isinstance(item, dict) or set(item) != _CHECKPOINT_KEYS:
        raise ValueError("invalid retention checkpoint fields")
    if item["kind"] != _CHECKPOINT_KIND or type(item["version"]) is not int or item["version"] != 1:
        raise ValueError("invalid retention checkpoint version")
    sequence = item["removed_through_sequence"]
    if type(sequence) is not int or sequence < 0:
        raise ValueError("invalid retention checkpoint sequence")
    for key in ("removed_head_hash", "hash"):
        if not isinstance(item[key], str) or not _HASH.fullmatch(item[key]):
            raise ValueError("invalid retention checkpoint hash")
    if sequence == 0 and item["removed_head_hash"] != _ZERO_HASH:
        raise ValueError("invalid retention checkpoint origin")
    if type(item["cleanup_pending"]) is not bool:
        raise ValueError("invalid retention checkpoint cleanup state")
    pruned_at, cutoff = _utc_timestamp(item["pruned_at"]), _utc_timestamp(item["cutoff"])
    if pruned_at - cutoff != timedelta(seconds=RETENTION_SECONDS):
        raise ValueError("invalid retention checkpoint cutoff")
    for key in ("partial_trace_ids", "partial_call_ids"):
        values = item[key]
        if (not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)
                or values != sorted(set(values))):
            raise ValueError("invalid retention checkpoint partial IDs")
    if hashlib.sha256(canonical({key: value for key, value in item.items() if key != "hash"})).hexdigest() != item["hash"]:
        raise ValueError("retention checkpoint hash failed")


@contextmanager
def _access_guard(directory: Path, *, write=False):
    """Serialize brief Windows reads and retention renames across processes.

    Windows MoveFileEx can reject a held destination even with delete sharing.
    A kernel mutex lets the writer retain its existing atomic os.replace path.
    Read validation and HTTP delivery must run after releasing this guard.
    Waits are bounded; file I/O itself remains cooperative, not hard preemption.
    """
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes
    identity = os.path.normcase(str(Path(directory).resolve()))
    name = "Local\\IndicesScratch-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    release = kernel.ReleaseMutex
    release.argtypes = [wintypes.HANDLE]
    release.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = wait(handle, _WRITER_WAIT_MILLISECONDS if write else _READER_WAIT_MILLISECONDS)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if result not in (0, 0x80):  # WAIT_OBJECT_0 or abandoned previous owner
            raise OSError("scratch access is temporarily busy")
        acquired = True
        yield
    finally:
        try:
            if acquired and not release(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close(handle)


@contextmanager
def _open_reader(path: Path, *, write=False):
    """Freeze a reader under the same brief guard used for retention changes."""
    with _access_guard(path.parent, write=write):
        with _shared_reader(path) as stream:
            yield stream


@contextmanager
def _shared_reader(path: Path):
    """Read one regular-file handle without following a replacement or link."""
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("scratch path must be a regular file")
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            yield stream
        return

    import ctypes
    from ctypes import wintypes
    import msvcrt

    class FileInformation(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                    ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    information = kernel.GetFileInformationByHandle
    information.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    information.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # GENERIC_READ; SHARE_READ | SHARE_WRITE | SHARE_DELETE; OPEN_EXISTING;
    # FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT (never follow links).
    handle = create(str(path), 0x80000000, 0x7, None, 3, 0x80 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        details = FileInformation()
        if not information(handle, ctypes.byref(details)):
            raise ctypes.WinError(ctypes.get_last_error())
        if details.attributes & (0x10 | 0x400):  # directory or reparse point
            raise ValueError("scratch path must be a regular file")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    # Ownership transfers to the CRT descriptor only after open_osfhandle.
    try:
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream


def _load(path: Path, *, max_bytes=None, writer=False):
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError("invalid scratch snapshot byte limit")
    previous = _ZERO_HASH
    sequence = 0
    checkpoint = None
    rows, lines = [], []
    bytes_read = 0
    previous_time = None
    if path.is_symlink() or path.is_junction():
        raise ValueError("scratch path must be a regular file")
    if path.exists():
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("scratch path must be a regular file")
        with _open_reader(path, write=writer) as reader:
            contents = reader.read() if max_bytes is None else reader.read(max_bytes + 1)
        bytes_read = len(contents)
        if max_bytes is not None and len(contents) > max_bytes:
            raise ValueError("scratch snapshot exceeds byte limit")
        with io.TextIOWrapper(io.BytesIO(contents), encoding="utf-8", newline="") as stream:
            for index, line in enumerate(stream):
                item = None
                try:
                    if not line.endswith("\n"):
                        raise ValueError("incomplete record boundary")
                    item = _decode(line)
                    if index == 0 and isinstance(item, dict) and item.get("kind") == _CHECKPOINT_KIND:
                        _validate_checkpoint(item)
                        checkpoint = item
                        sequence, previous = item["removed_through_sequence"], item["removed_head_hash"]
                        continue
                    _validate(item, sequence + 1, includes_hash=True)
                    # Also reject floating overflow (e.g. 1e999) parsed as inf.
                    canonical(item)
                    timestamp = _utc_timestamp(item["timestamp"])
                    if previous_time is not None and timestamp < previous_time:
                        raise ValueError("scratch timestamp moved backwards")
                    if checkpoint and not checkpoint["cleanup_pending"] and timestamp <= _utc_timestamp(checkpoint["cutoff"]):
                        raise ValueError("record predates completed retention checkpoint")
                except (ValueError, TypeError, UnicodeError):
                    if index == 0 and isinstance(item, dict) and item.get("kind") == _CHECKPOINT_KIND:
                        raise ValueError("scratch retention checkpoint failed") from None
                    raise ValueError(f"scratch structure failed at record {len(rows) + 1}") from None
                digest = item["hash"]
                if item.get("previous_hash") != previous or hashlib.sha256(canonical({key: value for key, value in item.items() if key != "hash"})).hexdigest() != digest:
                    raise ValueError(f"scratch chain failed at record {len(rows) + 1}")
                previous = digest
                sequence += 1
                previous_time = timestamp
                rows.append(item)
                lines.append(line)
    if checkpoint:
        for key, metadata_key in (("trace_id", "partial_trace_ids"), ("call_id", "partial_call_ids")):
            if set(checkpoint[metadata_key]) - _event_ids(rows, key):
                raise ValueError("scratch retention checkpoint has unavailable partial IDs")
    return {"rows": rows, "lines": lines, "checkpoint": checkpoint, "sequence": sequence,
            "head": previous, "last_timestamp": previous_time, "bytes_read": bytes_read}


def verify(path: Path) -> tuple[int, str]:
    """Return the retained event count and head; a retention header is not an event."""
    loaded = _load(Path(path))
    return len(loaded["rows"]), loaded["head"]


def read_records(path: Path) -> list[dict]:
    """Read a validated chain, excluding its optional local retention checkpoint."""
    return _load(Path(path))["rows"]


def retention_checkpoint(path: Path) -> dict | None:
    """Read the validated local truncation declaration, if present."""
    return _load(Path(path))["checkpoint"]


def read_snapshot(path: Path, *, max_bytes=None) -> dict:
    """Return rows and retention metadata from one bounded, strict file read.

    A trailing append without its newline is rejected along with all other
    structure/hash failures. Callers may report a temporarily unavailable
    snapshot; a verified prefix is never passed off as a complete read.
    """
    loaded = _load(Path(path), max_bytes=max_bytes)
    return {"records": loaded["rows"], "checkpoint": loaded["checkpoint"], "head": loaded["head"],
            "bytes_read": loaded["bytes_read"]}


def _event_ids(rows, key):
    return {row["fields"][key] for row in rows
            if isinstance(row["fields"].get(key), str) and row["fields"][key]}


class ScratchLog:
    """Single writer; production callers must hold the service InstanceLock.

    ``clock`` is an aware datetime supplier for deterministic offline tests.
    Startup cleans reserved orphan replacement files and expires old evidence.
    ``prune`` is synchronous; the service schedules it even without messages.
    """

    def __init__(self, directory: Path, *, clock=None):
        directory = Path(directory)
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("scratch directory must not be a link")
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory.resolve()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._poisoned = False
        self.stream = None
        self._last_now = None
        now = self._now()
        self.path = self.directory / (now.strftime("%Y-%m-%d") + ".jsonl")
        orphan_count = self._clean_orphans()
        self.startup_retention = self.prune(now=now)
        self.startup_retention["temporary_files_removed"] = orphan_count
        loaded = _load(self.path, writer=True)
        self.sequence, self.previous = loaded["sequence"], loaded["head"]
        self._last_timestamp = loaded["last_timestamp"]
        self.stream = self.path.open("a", encoding="utf-8", newline="\n")

    def _now(self, now=None):
        now = self._clock() if now is None else now
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("scratch clock must supply an aware datetime")
        now = now.astimezone(timezone.utc)
        if self._last_now is not None and now < self._last_now:
            raise ValueError("scratch clock moved backwards")
        return now

    def _safe_path(self, path):
        if path.parent != self.directory or path.is_symlink() or path.is_junction():
            raise ValueError("scratch retention path is unsafe")
        if path.resolve().parent != self.directory or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("scratch retention path is unsafe")

    def _clean_orphans(self):
        # The reserved namespace is only for uncommitted atomic replacements;
        # their plaintext age cannot be inferred safely from filesystem mtime.
        paths = sorted(path for path in self.directory.iterdir() if _TEMP_NAME.fullmatch(path.name))
        for path in paths:
            self._safe_path(path)
        for path in paths:
            with _access_guard(self.directory, write=True):
                self._safe_path(path)
                path.unlink()
        return len(paths)

    def _managed_paths(self):
        paths = []
        for path in self.directory.iterdir():
            if not _MANAGED_NAME.fullmatch(path.name):
                continue
            try:
                datetime.strptime(path.stem, "%Y-%m-%d")
            except ValueError:
                continue
            self._safe_path(path)
            paths.append(path)
        return sorted(paths)

    def _atomic_rewrite(self, path, contents):
        temporary = self.directory / (".scratch-retention-" + uuid.uuid4().hex + ".tmp")
        created = False
        try:
            with temporary.open("xb") as stream:
                created = True
                if stream.write(contents) != len(contents):
                    raise OSError("incomplete scratch retention write")
                stream.flush()
                os.fsync(stream.fileno())
            with _access_guard(self.directory, write=True):
                self._safe_path(path)
                if path == self.path and self.stream is not None:
                    # The observer closes its snapshot reader before this guard
                    # is acquired. Close the writer's append handle as well.
                    self.stream.close()
                os.replace(temporary, path)
                if path == self.path and self.stream is not None:
                    self.stream = self.path.open("a", encoding="utf-8", newline="\n")
        finally:
            if created and (temporary.exists() or temporary.is_symlink()):
                with _access_guard(self.directory, write=True):
                    self._safe_path(temporary)
                    temporary.unlink()

    def prune(self, *, now=None):
        """Remove rows with timestamp <= now-24h; retain their original suffix.

        Validation and planning precede every mutation. The atomicity boundary
        is one JSONL file, not the whole directory. I/O/validation failures latch
        the writer unavailable without repairing, retrying, or replaying data.
        """
        if self._poisoned:
            raise OSError("scratch writer unavailable after an I/O failure")
        if self.stream is not None and self.stream.closed:
            raise ValueError("scratch log is closed")
        try:
            now = self._now(now)
            cutoff = now - timedelta(seconds=RETENTION_SECONDS)
            plans = []
            expired_ids = {"trace_id": set(), "call_id": set()}
            retained_ids = {"trace_id": set(), "call_id": set()}
            for path in self._managed_paths():
                loaded = _load(path, writer=True)
                checkpoint = loaded["checkpoint"]
                if ((loaded["last_timestamp"] is not None and loaded["last_timestamp"] > now)
                        or (checkpoint and _utc_timestamp(checkpoint["pruned_at"]) > now)):
                    raise ValueError("scratch clock moved backwards")
                if path == self.path and self.stream is not None and (loaded["sequence"], loaded["head"]) != (self.sequence, self.previous):
                    raise ValueError("scratch writer state disagrees with the file")
                rows = loaded["rows"]
                removed = 0
                while removed < len(rows) and _utc_timestamp(rows[removed]["timestamp"]) <= cutoff:
                    removed += 1
                kept = rows[removed:]
                for key, metadata_key in (("trace_id", "partial_trace_ids"), ("call_id", "partial_call_ids")):
                    expired_ids[key].update(_event_ids(rows[:removed], key))
                    if checkpoint:
                        expired_ids[key].update(checkpoint[metadata_key])
                    retained_ids[key].update(_event_ids(kept, key))
                plans.append((path, loaded, removed, kept))
            partial = {key: expired_ids[key] & retained_ids[key] for key in expired_ids}
            result = {"files_examined": len(plans), "files_changed": 0, "records_removed": 0,
                      "files_removed": 0, "cutoff": cutoff.isoformat()}
            declarations, rewrites, deletions = [], [], []
            for path, loaded, removed, kept in plans:
                rows = loaded["rows"]
                checkpoint = loaded["checkpoint"]
                if not kept and path != self.path:
                    deletions.append((path, removed))
                    continue
                partial_traces = sorted(partial["trace_id"] & _event_ids(kept, "trace_id"))
                partial_calls = sorted(partial["call_id"] & _event_ids(kept, "call_id"))
                prior_traces = checkpoint["partial_trace_ids"] if checkpoint else []
                prior_calls = checkpoint["partial_call_ids"] if checkpoint else []
                metadata_changed = ((checkpoint is not None and
                                    (checkpoint["partial_trace_ids"] != partial_traces or checkpoint["partial_call_ids"] != partial_calls
                                     or checkpoint["cleanup_pending"]))
                                    or (checkpoint is None and bool(partial_traces or partial_calls)))
                if not removed and not metadata_changed:
                    continue
                anchor_sequence = rows[removed - 1]["sequence"] if removed else (checkpoint["removed_through_sequence"] if checkpoint else 0)
                anchor_hash = rows[removed - 1]["hash"] if removed else (checkpoint["removed_head_hash"] if checkpoint else _ZERO_HASH)
                header = {"kind": _CHECKPOINT_KIND, "version": 1, "removed_through_sequence": anchor_sequence,
                          "removed_head_hash": anchor_hash, "pruned_at": now.isoformat(), "cutoff": cutoff.isoformat(),
                          "partial_trace_ids": partial_traces, "partial_call_ids": partial_calls, "cleanup_pending": False}
                header["hash"] = hashlib.sha256(canonical(header)).hexdigest()
                _validate_checkpoint(header)
                contents = canonical(header) + b"\n" + "".join(loaded["lines"][removed:]).encode("utf-8")
                # Declare newly split IDs in every retained file before any
                # prefix containing their earlier events can be removed. Keep
                # the old partial declarations until their rows are removed.
                # This preparatory header may coexist with expired rows if a
                # later I/O failure interrupts the directory-wide operation.
                if set(partial_traces) - set(prior_traces) or set(partial_calls) - set(prior_calls):
                    declaration = {"kind": _CHECKPOINT_KIND, "version": 1,
                                   "removed_through_sequence": checkpoint["removed_through_sequence"] if checkpoint else 0,
                                   "removed_head_hash": checkpoint["removed_head_hash"] if checkpoint else _ZERO_HASH,
                                   "pruned_at": now.isoformat(), "cutoff": cutoff.isoformat(),
                                   "partial_trace_ids": sorted(set(prior_traces) | set(partial_traces)),
                                   "partial_call_ids": sorted(set(prior_calls) | set(partial_calls)), "cleanup_pending": True}
                    declaration["hash"] = hashlib.sha256(canonical(declaration)).hexdigest()
                    _validate_checkpoint(declaration)
                    declared_contents = canonical(declaration) + b"\n" + "".join(loaded["lines"]).encode("utf-8")
                    declarations.append((path, declared_contents))
                    if contents == declared_contents:
                        continue
                rewrites.append((path, contents, removed))
            # Cross-file partial declarations must become durable before the
            # earlier rows can disappear, including prefixes of retained files.
            changed_paths = set()
            for path, contents in declarations:
                self._atomic_rewrite(path, contents)
                changed_paths.add(path)
            for path, contents, removed in rewrites:
                self._atomic_rewrite(path, contents)
                changed_paths.add(path)
                result["records_removed"] += removed
            for path, removed in deletions:
                with _access_guard(self.directory, write=True):
                    self._safe_path(path)
                    path.unlink()
                changed_paths.add(path)
                result["files_removed"] += 1
                result["records_removed"] += removed
            result["files_changed"] = len(changed_paths)
            self._last_now = now
            return result
        except BaseException:
            self._poisoned = True
            raise

    def write(self, event: str, **fields):
        if self._poisoned:
            raise OSError("scratch writer unavailable after an I/O failure")
        if self.stream.closed:
            raise ValueError("scratch log is closed")
        try:
            now = self._now()
            if self._last_timestamp is not None and now < self._last_timestamp:
                raise ValueError("scratch clock moved backwards")
        except ValueError:
            self._poisoned = True
            raise
        sequence = self.sequence + 1
        item = {"version": 1, "sequence": sequence, "timestamp": now.isoformat(),
                "event": event, "fields": fields, "previous_hash": self.previous}
        # Fully validate and freeze caller-owned values before attempting I/O.
        content = canonical(item)
        item = _decode(content.decode("utf-8"))
        _validate(item, sequence, includes_hash=False)
        content = canonical(item)
        digest = hashlib.sha256(content).hexdigest()
        record = canonical({**item, "hash": digest}).decode("utf-8") + "\n"
        try:
            if self.stream.write(record) != len(record):
                raise OSError("incomplete scratch write")
            self.stream.flush()
            os.fsync(self.stream.fileno())
        except BaseException:
            # A failed append/flush/fsync may already have written bytes. Keep
            # the original file and refuse further appends rather than repair it.
            self._poisoned = True
            raise
        self.sequence = sequence
        self.previous = digest
        self._last_timestamp = self._last_now = now

    def close(self):
        if self.stream is not None:
            self.stream.close()
