"""Append-only request/response evidence. Hash chains detect local corruption.

These are observable payloads and receipts, never hidden chain-of-thought.
The local chain is not an externally anchored proof against deliberate rewriting.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re


_ZERO_HASH = "0" * 64
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")
_RECORD_KEYS = {"version", "sequence", "timestamp", "event", "fields", "previous_hash", "hash"}


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


def _validate(item, sequence, *, includes_hash):
    keys = _RECORD_KEYS if includes_hash else _RECORD_KEYS - {"hash"}
    if not isinstance(item, dict) or set(item) != keys:
        raise ValueError("invalid record fields")
    if type(item["version"]) is not int or item["version"] != 1:
        raise ValueError("invalid record version")
    if type(item["sequence"]) is not int or item["sequence"] != sequence:
        raise ValueError("invalid record sequence")
    timestamp = item["timestamp"]
    if not isinstance(timestamp, str) or not _UTC_TIME.fullmatch(timestamp):
        raise ValueError("invalid UTC timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        raise ValueError("invalid UTC timestamp") from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("invalid UTC timestamp")
    event = item["event"]
    if (not isinstance(event, str) or not event or event != event.strip() or not event.isprintable()):
        raise ValueError("invalid record event")
    if not isinstance(item["fields"], dict):
        raise ValueError("invalid event fields")
    for key in ("previous_hash", "hash") if includes_hash else ("previous_hash",):
        if not isinstance(item[key], str) or not _HASH.fullmatch(item[key]):
            raise ValueError("invalid record hash")


def verify(path: Path) -> tuple[int, str]:
    previous = _ZERO_HASH
    count = 0
    if path.exists():
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    if not line.endswith("\n"):
                        raise ValueError("incomplete record boundary")
                    item = _decode(line)
                    _validate(item, count + 1, includes_hash=True)
                    # Also reject floating overflow (e.g. 1e999) parsed as inf.
                    canonical(item)
                except (ValueError, TypeError, UnicodeError):
                    raise ValueError(f"scratch structure failed at record {count + 1}") from None
                digest = item.pop("hash")
                if item.get("previous_hash") != previous or hashlib.sha256(canonical(item)).hexdigest() != digest:
                    raise ValueError(f"scratch chain failed at record {count + 1}")
                previous = digest
                count += 1
    return count, previous


class ScratchLog:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")
        self.sequence, self.previous = verify(self.path)
        self.stream = self.path.open("a", encoding="utf-8", newline="\n")
        self._poisoned = False

    def write(self, event: str, **fields):
        if self._poisoned:
            raise OSError("scratch writer unavailable after an I/O failure")
        if self.stream.closed:
            raise ValueError("scratch log is closed")
        sequence = self.sequence + 1
        item = {"version": 1, "sequence": sequence, "timestamp": datetime.now(timezone.utc).isoformat(),
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

    def close(self):
        self.stream.close()
