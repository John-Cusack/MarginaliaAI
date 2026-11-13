"""Delete what has outlived its cap, and reconcile what is on disk.

Design 6 wants retention by an *independent* systemd timer rather than by the
idle-exiting service: a service that is not running cannot clean up, and a
service that crashes cannot be trusted to have. So the sweeper is its own
process, and it reconciles at every start, because the disk is the only thing
that knows what survived a crash.

Two rules, both non-negotiable:

* past the 14-day hard cap, an image is deleted; an unfinished page whose image
  went away is re-captured with a new `capture_id`, and results bound to the old
  one are discarded rather than reused;
* a file in the page store that no committed capture claims is deleted. Files
  appear there only through a commit; anything else is debris from a crash.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .store import IMAGE_TTL_DAYS, Store

log = logging.getLogger("marginalia_ocr.sweeper")


@dataclass
class SweepReport:
    expired_deleted: int = 0
    debris_deleted: int = 0
    recapture_requested: int = 0
    jobs_touched: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "expired_deleted": self.expired_deleted,
            "debris_deleted": self.debris_deleted,
            "recapture_requested": self.recapture_requested,
            "jobs_touched": list(self.jobs_touched),
        }


def sweep(store: Store, *, now: datetime | None = None, ttl_days: int = IMAGE_TTL_DAYS) -> SweepReport:
    """One pass: expire, then reconcile. Safe to run at any time, any number of times."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    report = SweepReport()
    touched: set[str] = set()

    for capture in store.expired_captures(moment):
        capture.path.unlink(missing_ok=True)
        # The row stays and is marked discarded. It is the record that this page
        # was read under the old capture - exactly the results design 6 says to
        # discard - and it is what lets a re-capture of the same sequence land.
        store.discard_captures_for(capture.job_id, capture.capture_id)
        report.expired_deleted += 1
        touched.add(capture.job_id)
        # An unfinished page whose image expired is re-captured; a final one is
        # past caring, and its text no longer depends on the image.
        if store.state_of(capture.job_id) != "final":
            store.request_recapture(
                capture.job_id,
                position=capture.seq,
                reason="image expired before the page was finalized",
                dpr=int(store.job(capture.job_id)["dpr"]),
            )
            report.recapture_requested += 1

    for job_id in store.job_ids():
        directory = store.page_store / job_id
        if not directory.exists():
            continue
        claimed = {
            capture.path.name for capture in store.captures(job_id, include_discarded=True)
        }
        for child in directory.iterdir():
            if child.is_file() and child.name not in claimed:
                child.unlink()
                report.debris_deleted += 1
                touched.add(job_id)
        if not any(directory.iterdir()):
            directory.rmdir()

    report.jobs_touched = tuple(sorted(touched))
    log.info("sweep", extra=report.as_dict())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="delete expired page images and reconcile the store")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--page-store", required=True, type=Path)
    parser.add_argument("--ttl-days", type=int, default=IMAGE_TTL_DAYS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = Store(args.db, args.page_store)
    report = sweep(store, ttl_days=args.ttl_days)
    log.info("swept", extra=report.as_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
