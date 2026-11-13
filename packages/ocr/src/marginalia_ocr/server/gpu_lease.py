"""A host-local GPU lease: one holder at a time, with a VRAM budget.

The lease exists because two of our own processes loading models at once is how
a GPU ends up with neither of them working, and because the operator's TTS
workloads live outside any lease we control — so exclusion alone is not enough
and a *free-VRAM check* is taken after acquiring. Both failures are closed: the
holder does not proceed.

flock, not a pidfile: a pidfile survives a crash and a stale lock does not.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

#: Where the lock lives. Host-local, like the GPU it guards.
DEFAULT_LEASE_PATH = Path(os.environ.get("MARGINALIA_GPU_LEASE", "/tmp/marginalia-gpu.lease"))


class LeaseError(RuntimeError):
    """The lease could not be taken, or the GPU cannot honour the budget."""


class InsufficientVram(LeaseError):
    pass


def free_vram_mib(index: int = 0) -> int | None:
    """Free VRAM on one GPU, from nvidia-smi; `None` means it could not be read.

    `None` is not "plenty": a caller that cannot measure the GPU must fail
    closed, which is why this returns None rather than a guess.
    """
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
                "-i",
                str(index),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    try:
        total, used = (int(part.strip()) for part in out.splitlines()[0].split(","))
    except ValueError:
        return None
    return max(0, total - used)


@dataclass
class Lease:
    """Take it, use the GPU, give it back.

    `budget_mib` is what the holder intends to load. The check happens after
    acquiring the lock, so a holder never reads a VRAM figure that another
    lease-holder is about to consume.
    """

    path: Path = DEFAULT_LEASE_PATH
    holder: str = "unknown"
    budget_mib: int = 0
    gpu_index: int = 0
    timeout: float = 0.0

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._fd: int | None = None

    def acquire(self) -> Lease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    holder = self.path.read_text().strip() if self.path.exists() else ""
                    raise LeaseError(
                        f"the GPU lease is held by {holder or 'another process'}"
                    ) from exc
                time.sleep(0.05)
        os.ftruncate(fd, 0)
        os.write(fd, f"{self.holder} budget={self.budget_mib}MiB pid={os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd

        if self.budget_mib:
            available = free_vram_mib(self.gpu_index)
            if available is None:
                self.release()
                raise InsufficientVram("free VRAM could not be read, so the budget cannot be honoured")
            if available < self.budget_mib:
                self.release()
                raise InsufficientVram(
                    f"{available} MiB free, {self.budget_mib} MiB asked for"
                )
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> Lease:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
