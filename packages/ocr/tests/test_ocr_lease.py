"""The GPU lease: one holder, and a budget the GPU can actually honour.

Exclusion alone is not enough. The operator's TTS jobs are outside any lease we
control, so a holder that has taken the lock still has to look at the card before
loading. Both failures are closed - a holder that cannot prove it has the memory
does not proceed - because the failure mode we are avoiding is two of our own
processes each loading a model into a card that fits one.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from marginalia_ocr.server import gpu_lease
from marginalia_ocr.server.gpu_lease import InsufficientVram, Lease

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def lease_path(tmp_path: Path) -> Path:
    return tmp_path / "gpu.lease"


def test_a_second_process_fails_closed_while_the_lease_is_held(lease_path: Path):
    """flock, so a crash releases it: the holder is not a pidfile."""
    child = """
import sys
from pathlib import Path
from marginalia_ocr.server.gpu_lease import Lease, LeaseError
try:
    Lease(path=Path(sys.argv[1]), holder="child", budget_mib=0).acquire()
except LeaseError as exc:
    print(f"refused: {exc}")
    raise SystemExit(3)
print("acquired")
"""
    with Lease(path=lease_path, holder="parent", budget_mib=0):
        finished = subprocess.run(
            [sys.executable, "-c", child, str(lease_path)], capture_output=True, text=True
        )
        assert finished.returncode == 3, finished.stdout + finished.stderr
        assert "refused" in finished.stdout

    after = subprocess.run([sys.executable, "-c", child, str(lease_path)], capture_output=True, text=True)
    assert after.returncode == 0 and "acquired" in after.stdout


def test_the_holder_is_named_in_the_lock_file(lease_path: Path):
    with Lease(path=lease_path, holder="ocr-service", budget_mib=0):
        assert "ocr-service" in lease_path.read_text()


def test_a_budget_the_card_cannot_honour_fails_closed(lease_path: Path, monkeypatch):
    monkeypatch.setattr(gpu_lease, "free_vram_mib", lambda index=0: 6_000)
    with pytest.raises(InsufficientVram) as caught:
        Lease(path=lease_path, holder="vlm", budget_mib=12_000).acquire()
    assert "6000 MiB free" in str(caught.value)

    # and the failed attempt released the lock: the next holder is not blocked
    with Lease(path=lease_path, holder="tesseract", budget_mib=0):
        assert "tesseract" in lease_path.read_text()


def test_an_unreadable_vram_figure_is_not_a_pass(lease_path: Path, monkeypatch):
    """A card that cannot be measured is a card whose budget cannot be honoured."""
    monkeypatch.setattr(gpu_lease, "free_vram_mib", lambda index=0: None)
    with pytest.raises(InsufficientVram) as caught:
        Lease(path=lease_path, holder="vlm", budget_mib=100).acquire()
    assert "could not be read" in str(caught.value)
    with Lease(path=lease_path, holder="next", budget_mib=0):
        assert "next" in lease_path.read_text()


def test_a_zero_budget_takes_the_lease_without_touching_the_card(lease_path: Path):
    """CPU-only stages (Tesseract) still deserve serialization, not a VRAM claim."""
    with Lease(path=lease_path, holder="tesseract", budget_mib=0) as lease:
        assert lease._fd is not None
    assert lease._fd is None


def test_release_is_idempotent(lease_path: Path):
    lease = Lease(path=lease_path, holder="x", budget_mib=0).acquire()
    lease.release()
    lease.release()
    with Lease(path=lease_path, holder="y", budget_mib=0):
        assert "y" in lease_path.read_text()
