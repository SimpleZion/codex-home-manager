from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from pathlib import Path


class ProbeStopped(RuntimeError):
    pass


class ProbeBudget:
    """Cooperative budget; an OS call already in progress cannot be interrupted."""

    def __init__(self, seconds: float = 5, maximum_bytes: int = 1024 * 1024 * 1024):
        self.deadline = time.monotonic() + seconds
        self.maximum_bytes = maximum_bytes
        self.read_bytes = 0
        self.cancelled = threading.Event()

    def check(self) -> None:
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise ProbeStopped("Runtime inspection cancelled or deadline exceeded")

    def reserve(self, size: int) -> None:
        self.check()
        if size > self.maximum_bytes - self.read_bytes:
            self.cancelled.set()
            raise ProbeStopped("Runtime inspection aggregate byte limit exceeded")
        self.read_bytes += size

    def read(self, path: Path, *, maximum_bytes: int = 1024 * 1024, digest: bool = False) -> bytes | str:
        self.check()
        before = path.stat()
        self.check()
        limit = min(maximum_bytes, self.maximum_bytes - self.read_bytes)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            self.cancelled.set()
            raise ProbeStopped("Runtime inspection file or aggregate byte limit exceeded")
        value = hashlib.sha256() if digest else bytearray()
        consumed = 0
        with path.open("rb") as stream:
            while True:
                self.check()
                chunk = stream.read(min(65536, limit - consumed + 1))
                self.read_bytes += len(chunk)
                consumed += len(chunk)
                self.check()
                if consumed > limit:
                    self.cancelled.set()
                    raise ProbeStopped("Runtime inspection file grew beyond byte limit")
                if not chunk:
                    break
                value.update(chunk) if digest else value.extend(chunk)
            opened = os.fstat(stream.fileno())
        self.check()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if consumed != before.st_size or identity(before) != identity(opened) or identity(before) != identity(path.stat()):
            raise ProbeStopped("Runtime inspection file changed while reading")
        return value.hexdigest() if digest else bytes(value)


def probe_text(path: Path, budget: ProbeBudget | None, *, errors: str = "strict", maximum_bytes: int = 1024 * 1024) -> str:
    if budget is None:
        return path.read_text(encoding="utf-8", errors=errors)
    return budget.read(path, maximum_bytes=maximum_bytes).decode("utf-8", errors=errors)
