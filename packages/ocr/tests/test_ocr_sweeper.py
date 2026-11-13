"""The sweeper: the 14-day cap, and reconciliation after a crash.

Retention is a timer rather than part of the service because the service exits
when idle: a process that is not running cannot be the thing that guarantees a
cap. The sweeper therefore has to be safe to run at any moment, idempotent, and
honest about what it did - counts, never images or text.
"""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
    timedelta,  # noqa: TC003
)
from typing import TYPE_CHECKING

import pytest
from marginalia_ocr.server.store import IMAGE_TTL_DAYS, Store
from marginalia_ocr.server.sweeper import sweep
from marginalia_ocr.wire import STATES

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "jobs.sqlite3", tmp_path / "pages")


def job_with_capture(store: Store, job_id: str = "B0-wp1", *, ttl_days: int = IMAGE_TTL_DAYS) -> str:
    store.create_job(job_id, "B0", 3, "wp1")
    store.commit_capture(
        job_id=job_id,
        seq=0,
        capture_id="c0",
        image_sha256="a" * 64,
        image_bytes=b"png",
        ttl_days=ttl_days,
    )
    return job_id


def test_an_expired_image_is_deleted_and_the_page_owes_a_recapture(store: Store):
    job_id = job_with_capture(store, ttl_days=0)
    capture = store.captures(job_id)[0]
    assert capture.path.exists()

    report = sweep(store, now=datetime.now(UTC) + timedelta(minutes=1))

    assert report.expired_deleted == 1
    assert not capture.path.exists()
    assert store.captures(job_id) == ()
    assert store.recapture_requests(job_id) == ((0, "image expired before the page was finalized", 3),)
    assert job_id in report.jobs_touched


def test_a_final_job_loses_its_images_without_owing_anything(store: Store):
    """A final book's text does not depend on its images any more."""
    job_id = job_with_capture(store)   # live image: this job is finished, not expired
    for state in STATES[1:]:
        if state == "images_deleted":
            store.delete_images(job_id)
        store.advance(job_id, state)
    assert store.state_of(job_id) == "final"

    # a capture recorded after finalization: the sweeper must still not queue work
    store.commit_capture(job_id=job_id, seq=9, capture_id="late", image_sha256="b" * 64,
                         image_bytes=b"png", ttl_days=0)
    report = sweep(store, now=datetime.now(UTC) + timedelta(minutes=1))
    assert report.expired_deleted == 1
    assert report.recapture_requested == 0
    assert store.recapture_requests(job_id) == ()


def test_a_live_image_is_left_alone(store: Store):
    job_id = job_with_capture(store)
    report = sweep(store)
    assert report.expired_deleted == 0
    assert store.captures(job_id)[0].path.exists()


def test_reconciliation_deletes_a_file_no_capture_claims(store: Store):
    """Files appear in the page store only through a commit; anything else is debris."""
    job_id = job_with_capture(store)
    debris = store.job_dir(job_id) / "000000-crash.part"
    debris.write_bytes(b"half a screen")
    keep = store.captures(job_id)[0].path

    report = sweep(store)

    assert report.debris_deleted == 1
    assert not debris.exists()
    assert keep.exists(), "the committed screen was not touched"


def test_a_sweep_is_idempotent_and_leaves_no_empty_directories(store: Store):
    job_id = job_with_capture(store, ttl_days=0)
    sweep(store, now=datetime.now(UTC) + timedelta(days=1))
    again = sweep(store, now=datetime.now(UTC) + timedelta(days=2))
    assert again.expired_deleted == 0 and again.debris_deleted == 0
    assert not (store.page_store / job_id).exists()


def test_an_expired_page_can_be_captured_again_under_a_new_capture_id(store: Store):
    """Expiry mid-book is a design case, not an error path (design 6).

    The sweeper deletes the image of an unfinished page and the walk takes that
    screen again. The new capture has a new `capture_id` and the same sequence,
    which must replace the discarded one rather than be refused as a conflict -
    otherwise the page could never be re-captured and the book could never
    finish.
    """
    job_id = job_with_capture(store, ttl_days=0)
    sweep(store, now=datetime.now(UTC) + timedelta(minutes=1))
    assert store.recapture_requests(job_id) == ((0, "image expired before the page was finalized", 3),)

    store.commit_capture(
        job_id=job_id,
        seq=0,
        capture_id="c0-again",
        image_sha256="b" * 64,
        image_bytes=b"png again",
    )
    captures = store.captures(job_id)
    assert [capture.capture_id for capture in captures] == ["c0-again"]
    assert captures[0].path.read_bytes() == b"png again"
    # the discarded one is still on record: the page was once read from it
    assert [c.capture_id for c in store.captures(job_id, include_discarded=True)] == ["c0-again"]
    store.forget_recapture(job_id, 0)
    assert store.recapture_requests(job_id) == ()


def test_a_live_capture_of_the_same_sequence_is_still_a_conflict(store: Store):
    """Two live captures for one sequence would make 'the screen' ambiguous."""
    job_id = job_with_capture(store)
    with pytest.raises(Exception) as caught:
        store.commit_capture(
            job_id=job_id, seq=0, capture_id="other", image_sha256="b" * 64, image_bytes=b"png"
        )
    assert "already committed" in str(caught.value)
