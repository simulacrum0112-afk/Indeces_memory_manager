"""Cooperative writer exclusion and bounded recovery of one durable snapshot."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
import uuid

from .contracts import GovernedError


_WINDOWS_RECOVERY = os.name == "nt"
_registry_guard = threading.Lock()
_writers = {}
_UNSET = object()


def _ownership_failure(error=None):
    if error is None:
        error = GovernedError("requery_ledger_identity_changed", remote_usage_unknown=False)
    error.ledger_ownership_lost = True
    return error


def _regular_file(info):
    return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400


@contextmanager
def _writer_lease(path):
    """Every helper writer uses the same nonblocking process/kernel lease.

    Keep the byte-lock anchor: unlinking it could split concurrent lock owners
    across different file objects. This does not police unrelated OS writers.
    """
    key = os.path.normcase(os.path.abspath(path))
    with _registry_guard:
        entry = _writers.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    acquired = entry[0].acquire(blocking=False)
    descriptor = None
    kernel_locked = False
    try:
        if not acquired:
            raise _ownership_failure(GovernedError("requery_ledger_writer_busy", remote_usage_unknown=False))
        anchor = path.with_name(path.name + ".writer.lock")
        try:
            baseline = anchor.stat(follow_symlinks=False)
        except FileNotFoundError:
            baseline = None
        if baseline is not None and not _regular_file(baseline):
            raise _ownership_failure()
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if baseline is None:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(anchor, flags, 0o666)
        opened, current = os.fstat(descriptor), anchor.stat(follow_symlinks=False)
        identity = lambda info: (info.st_dev, info.st_ino)
        if (not _regular_file(opened) or not _regular_file(current)
                or identity(opened) != identity(current)
                or baseline is not None and identity(baseline) != identity(opened)):
            raise _ownership_failure()
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise _ownership_failure(GovernedError("requery_ledger_writer_busy", remote_usage_unknown=False)) from None
        kernel_locked = True
        yield
    finally:
        preserve_failure = sys.exc_info()[0] is not None
        close_error = None
        if descriptor is not None:
            try:
                if kernel_locked:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                try:
                    os.close(descriptor)
                except OSError as error:
                    close_error = error
        if acquired:
            entry[0].release()
        with _registry_guard:
            entry[1] -= 1
            if entry[1] == 0:
                del _writers[key]
        if close_error is not None and not preserve_failure:
            raise _failure(close_error, "close") from None


def _fingerprint(path):
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not _regular_file(before):
        raise _ownership_failure()
    # Both the reader and the writer handles are closed before each rename.
    try:
        with path.open("rb") as stream:
            checksum = hashlib.sha256()
            for block in iter(lambda: stream.read(65536), b""):
                checksum.update(block)
        after = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise _ownership_failure() from None
    fields = lambda value: (value.st_dev, value.st_ino, value.st_size,
                            value.st_mtime_ns, value.st_ctime_ns, value.st_mode)
    if fields(before) != fields(after):
        raise _ownership_failure()
    return fields(after), checksum.digest()


def _unchanged(path, expected):
    try:
        return _fingerprint(path) == expected
    except (OSError, GovernedError):
        return False


def _failure(error, operation):
    result = GovernedError("requery_ledger_write_failed", remote_usage_unknown=False)
    result.ledger_io_failure = {
        "operation": operation,
        "errno": error.errno if type(error.errno) is int else None,
        "winerror": getattr(error, "winerror", None)
                    if type(getattr(error, "winerror", None)) is int else None,
    }
    return result


def atomic_json(path, value, *, allow_recovery=True, expected_digest=_UNSET):
    """Write once; retry only an unchanged Windows rename denied with 13/5.

    Fingerprints detect drift between attempts under the cooperative writer
    lease. They are not a filesystem CAS against arbitrary external programs.
    Failed pending files are retained, including any externally changed file.
    """
    path = Path(path)
    operation = "open"
    try:
        with _writer_lease(path):
            target = _fingerprint(path)
            if expected_digest is not _UNSET:
                actual_digest = target[1].hex() if target is not None else None
                if actual_digest != expected_digest:
                    raise _ownership_failure()
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".pending")
            stream = temporary.open("x", encoding="utf-8", newline="\n")
            try:
                operation = "write"
                stream.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
                operation = "flush"
                stream.flush()
                operation = "fsync"
                os.fsync(stream.fileno())
            except BaseException:
                try:
                    stream.close()
                except BaseException:
                    pass
                raise
            operation = "close"
            stream.close()
            operation = "open"
            pending = _fingerprint(temporary)
            if pending is None or not _unchanged(path, target) or not _unchanged(temporary, pending):
                raise _ownership_failure()
            operation = "replace"
            first_error = None
            intentional_wait = 0.0
            for attempt in range(1, 4):
                try:
                    temporary.replace(path)
                except OSError as error:
                    if first_error is None:
                        first_error = error
                    if not _unchanged(path, target) or not _unchanged(temporary, pending):
                        raise _ownership_failure(_failure(first_error, "replace")) from None
                    if (not _WINDOWS_RECOVERY or allow_recovery is not True or attempt == 3
                            or type(error.errno) is not int or error.errno != 13
                            or type(getattr(error, "winerror", None)) is not int or error.winerror != 5):
                        raise _failure(first_error, "replace") from None
                    wait = 0.01 if attempt == 1 else 0.02
                    time.sleep(wait)
                    intentional_wait += wait
                    if not _unchanged(path, target) or not _unchanged(temporary, pending):
                        raise _ownership_failure(_failure(first_error, "replace")) from None
                else:
                    if first_error is not None:
                        return {"first_failure": _failure(first_error, "replace").ledger_io_failure,
                                "replace_attempts": attempt, "intentional_wait_seconds": intentional_wait}
                    return None
    except OSError as error:
        raise _failure(error, operation) from None
