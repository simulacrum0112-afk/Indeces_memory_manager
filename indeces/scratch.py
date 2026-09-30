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


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def verify(path: Path) -> tuple[int, str]:
    previous = "0" * 64
    count = 0
    if path.exists():
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
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

    def write(self, event: str, **fields):
        self.sequence += 1
        item = {"version": 1, "sequence": self.sequence, "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event, "fields": fields, "previous_hash": self.previous}
        digest = hashlib.sha256(canonical(item)).hexdigest()
        self.stream.write(canonical({**item, "hash": digest}).decode("utf-8") + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.previous = digest

    def close(self):
        self.stream.close()
