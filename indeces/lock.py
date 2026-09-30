"""Kernel-owned single-instance lease, released on normal exit or process death."""
from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.stream = (state_dir / "runtime.lock").open("a+b")
        self.stream.seek(0)
        if os.name == "nt":
            import msvcrt
            # Windows denies reading an already locked byte. Check file size
            # without reading that region, then let locking report contention.
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            try:
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.stream.close()
                raise RuntimeError("another Indeces instance owns this state directory") from None
        else:
            import fcntl
            try:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.stream.close()
                raise RuntimeError("another Indeces instance owns this state directory") from None

    def close(self):
        self.stream.close()
