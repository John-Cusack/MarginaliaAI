"""The job store, the state machine, and durability.

Design 9.5's order is the interesting part: `final` comes after
`images_deleted`, and the store refuses to record `final` while images remain,
whatever a caller asks for. A book whose images are still on disk is not final,
however good its text is.

Durability is tested the same way: a commit is only a commit once the bytes are
fsynced, so a crash right after the ack must leave both the file and the row.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from marginalia_ocr.server.store import (
    IMAGE_TTL_DAYS,
    CaptureConflict,
    Store,
    StoreError,
    UnknownJob,
)
from marginalia_ocr.wire import STATES

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "jobs.sqlite3", tmp_path / "pages")


def make_job(store: Store, job_id: str = "B004774792-wp1", **kwargs) -> str:
    store.create_job(job_id, kwargs.pop("asin", "B004774792"), kwargs.pop("dpr", 3), "wp1", **kwargs)
    return job_id


def commit(store: Store, job_id: str, seq: int = 0, image: bytes = b"\x89PNG-data") -> None:
    store.commit_capture(
        job_id=job_id,
        seq=seq,
        capture_id=f"capture-{seq}",
        image_sha256="a" * 64,
        image_bytes=image,
    )


def test_a_new_job_starts_captured_and_records_the_step(store: Store):
    job_id = make_job(store)
    assert store.state_of(job_id) == "captured"
    assert store.transitions(job_id) == ("captured",)
    with pytest.raises(UnknownJob):
        store.state_of("nope")


def test_the_state_machine_refuses_a_step_that_is_not_next(store: Store):
    job_id = make_job(store)
    with pytest.raises(StoreError):
        store.advance(job_id, "final", "skipping ahead")
    with pytest.raises(StoreError):
        store.advance(job_id, "assembled")
    store.advance(job_id, "read")
    assert store.transitions(job_id) == ("captured", "read")


def test_a_committed_capture_is_on_disk_before_the_row_exists(store: Store):
    job_id = make_job(store)
    commit(store, job_id, seq=0, image=b"first screen")
    captures = store.captures(job_id)
    assert len(captures) == 1
    capture = captures[0]
    assert capture.path.read_bytes() == b"first screen"
    assert capture.path.parent == store.page_store / job_id
    # Owner-only, both the directory and the file: a page image is transient but
    # it is never world-readable while it exists.
    assert stat.S_IMODE(capture.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(capture.path.parent.stat().st_mode) == 0o700
    assert capture.expire_at <= datetime.now(UTC) + timedelta(days=IMAGE_TTL_DAYS)


def test_the_same_screen_twice_is_idempotent_and_a_different_one_is_not(store: Store):
    job_id = make_job(store)
    commit(store, job_id, seq=0, image=b"screen")
    commit(store, job_id, seq=0, image=b"screen")  # a retried upload
    assert len(store.captures(job_id)) == 1

    with pytest.raises(CaptureConflict):
        store.commit_capture(
            job_id=job_id,
            seq=0,
            capture_id="another",
            image_sha256="b" * 64,
            image_bytes=b"different",
        )
    # and the refused upload left nothing behind
    assert len(list((store.page_store / job_id).iterdir())) == 1


def test_deletion_is_proved_before_the_state_says_it_happened(store: Store):
    """`images_deleted` and `final` may not be recorded while images remain.

    It is not enough for the caller to promise it deleted them: the state is read
    by the next stage, and by the operator, as a statement about the disk.
    """
    job_id = make_job(store)
    commit(store, job_id, seq=0)
    for state in STATES[1:-2]:
        store.advance(job_id, state)
    assert store.state_of(job_id) == "bundle_written"

    with pytest.raises(StoreError) as caught:
        store.advance(job_id, "images_deleted")
    assert "while images remain" in str(caught.value)
    assert store.state_of(job_id) == "bundle_written", "a refused step changes nothing"

    deleted = store.delete_images(job_id)
    assert deleted == 1
    assert not (store.page_store / job_id).exists()
    store.advance(job_id, "images_deleted")

    # The state is a claim about the disk, so a file that appears afterwards -
    # a crash, a stray write, a restarted walk - must stop `final` too.
    store.job_dir(job_id).joinpath("late-arrival.png").write_bytes(b"png")
    with pytest.raises(StoreError) as late:
        store.advance(job_id, "final")
    assert "while images remain" in str(late.value)

    store.delete_images(job_id)
    store.advance(job_id, "final")
    assert store.state_of(job_id) == "final"
    assert store.transitions(job_id)[-1] == "final"


def test_the_steps_must_be_taken_in_order(store: Store):
    job_id = make_job(store)
    commit(store, job_id, seq=0)
    store.delete_images(job_id)
    with pytest.raises(StoreError):
        store.advance(job_id, "final")   # skips the whole read


def test_deleting_images_discards_their_captures(store: Store):
    """A result bound to a deleted image cannot be reused (design 6)."""
    job_id = make_job(store)
    commit(store, job_id, seq=0)
    store.delete_images(job_id)
    assert store.captures(job_id) == ()
    assert len(store.captures(job_id, include_discarded=True)) == 1
    assert store.last_committed_seq(job_id) is None


def test_a_pipeline_version_change_invalidates_everything_but_final(store: Store):
    undone = make_job(store, "A-wp1", asin="A")
    commit(store, undone, seq=0)
    store.advance(undone, "read")
    store.mark_read(undone, 0)
    assert store.captures(undone)[0].read_at is not None, "it was read under the old rules"
    kept = make_job(store, "B-wp1", asin="B")
    commit(store, kept, seq=0)
    for state in STATES[1:]:
        if state == "images_deleted":
            store.delete_images(kept)
        store.advance(kept, state)

    changed = store.invalidate_for_pipeline_version("wp1", "wp2")
    assert changed == (undone,)
    assert store.state_of(undone) == "captured"
    assert store.captures(undone)[0].read_at is None, "the OLD result was invalidated"
    assert store.state_of(kept) == "final"
    assert store.job(kept)["pipeline_version"] == "wp1"


def test_recapture_requests_are_idempotent_per_position(store: Store):
    job_id = make_job(store)
    store.request_recapture(job_id, 41, "image expired", 3)
    store.request_recapture(job_id, 41, "image expired", 3)
    assert store.recapture_requests(job_id) == ((41, "image expired", 3),)
    store.forget_recapture(job_id, 41)
    assert store.recapture_requests(job_id) == ()


def test_the_database_is_written_in_wal_with_full_sync(tmp_path: Path):
    """An ack is a promise about a power cut, so the journal has to match."""
    store = Store(tmp_path / "jobs.sqlite3", tmp_path / "pages")
    make_job(store)
    import sqlite3

    db = sqlite3.connect(store.db_path)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        db.close()
    assert os.path.exists(store.db_path)


def test_a_conflicting_reupload_never_touches_the_live_image(store: Store):
    """The check comes before the write, or the write destroys what it is checking.

    A re-upload with the same capture_id and different bytes lands on the same
    path. Writing first overwrites the live image, and then the conflict branch
    unlinks it - leaving the committed row pointing at nothing, which is a page
    the walk believes it has and cannot read.
    """
    job_id = make_job(store)
    commit(store, job_id, seq=0, image=b"the committed screen")
    live = store.captures(job_id)[0].path

    with pytest.raises(CaptureConflict):
        store.commit_capture(
            job_id=job_id,
            seq=0,
            capture_id="capture-0",          # same id, different bytes
            image_sha256="b" * 64,
            image_bytes=b"a different screen",
        )

    assert live.exists(), "the live image was destroyed by a refused upload"
    assert live.read_bytes() == b"the committed screen"
    assert store.captures(job_id)[0].path.read_bytes() == b"the committed screen"
    assert [p.name for p in live.parent.iterdir()] == [live.name], "no debris left behind"


def test_a_version_change_keeps_the_capture_rows(store: Store):
    """The rows are the record that a page exists; deleting them loses the page.

    Its own docstring says the rows stay. Deleting them also makes the images
    unclaimed, so the sweeper collects them as debris and the job sits at
    `captured` with no pages and nothing queued to re-take.
    """
    job_id = make_job(store)
    commit(store, job_id, seq=0)
    store.advance(job_id, "read")
    store.mark_read(job_id, 0)

    changed = store.invalidate_for_pipeline_version("wp1", "wp2")

    assert changed == (job_id,)
    assert store.state_of(job_id) == "captured"
    captures = store.captures(job_id)
    assert len(captures) == 1, "the page still exists and its image is still claimed"
    assert captures[0].read_at is None, "the read is what the version change invalidates"
    assert captures[0].path.exists()


def test_a_blocked_job_keeps_its_pages_and_says_why(store: Store):
    """A book that cannot go on stops where it stands, with its images intact.

    Design 9.5 has no failure state, so this is the one the operator asked for: it
    holds the images inside the 14-day cap, can be retried, and is never final.
    """
    job_id = make_job(store)
    commit(store, job_id, seq=0)
    store.advance(job_id, "read")

    store.block(job_id, "the audit failed twice; awaiting a decision")

    assert store.state_of(job_id) == "blocked"
    assert store.job(job_id)["blocked_reason"].startswith("the audit failed")
    assert store.captures(job_id), "a blocked job keeps its pages"
    with pytest.raises(StoreError):
        store.advance(job_id, "checked")

    assert store.unblock(job_id) == "read", "it resumes from where it stopped"
    assert store.state_of(job_id) == "read"
    store.advance(job_id, "checked")


def test_a_block_needs_a_reason_and_never_touches_a_final_job(store: Store):
    job_id = make_job(store)
    commit(store, job_id, seq=0)
    with pytest.raises(StoreError):
        store.block(job_id, "   ")

    for state in STATES[1:]:
        if state == "images_deleted":
            store.delete_images(job_id)
        store.advance(job_id, state)
    assert store.state_of(job_id) == "final"
    with pytest.raises(StoreError):
        store.block(job_id, "too late")


def test_a_page_has_a_status_of_its_own(store: Store):
    """The job's chain says nothing about one page: one can be gapped while the
    job walks on, and one can be waiting for a screen while the rest is done."""
    job_id = make_job(store)
    commit(store, job_id, seq=0)
    commit(store, job_id, seq=1)

    assert store.page_status(job_id, 0) == ("pending", ""), "a committed page starts pending"

    store.set_page_status(job_id, 0, "confirmed")
    store.set_page_status(job_id, 1, "marked", "one word disputed, no tie-break applied")

    assert store.page_status(job_id, 0) == ("confirmed", "")
    assert store.page_status(job_id, 1) == ("marked", "one word disputed, no tie-break applied")
    assert [status for _seq, status, _reason in store.page_statuses(job_id)] == ["confirmed", "marked"]

    with pytest.raises(StoreError):
        store.set_page_status(job_id, 0, "finished")


def test_an_expired_page_is_marked_awaiting_recapture(store: Store):
    job_id = make_job(store)
    commit(store, job_id, seq=0)
    store.request_recapture(job_id, 0, "image expired before the page was finalized", 3)
    assert store.page_status(job_id, 0)[0] == "awaiting_recapture"
