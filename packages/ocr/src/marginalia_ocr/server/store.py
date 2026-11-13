"""The job store, and the state machine a book moves through.

Design 9.5: `captured -> read -> checked -> assembled -> audited(pass) ->
bundle_written (hashed) -> images_deleted -> final`. Every step is recorded, a
crash resumes from the last recorded step, and **`final` is never set before
deletion succeeds** — a book whose images are still on disk is not final, however
good its text is. A pipeline-version change invalidates every non-final book's
reads, audits and bundles, because a reading is only meaningful under the rules
that produced it.

Durability is the point of this module, not an afterthought: SQLite runs in WAL
with `synchronous=FULL`, and the page store fsyncs the file and its directory
before the store records the capture. The pack's `CommitAck` means "this screen
survives a power cut", so nothing may ack before that is true.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..wire import STATES, State

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The hard cap from design 6. Images older than this are deleted by the sweeper
#: whether or not the book went final; nothing may extend it.
IMAGE_TTL_DAYS = 14


class StoreError(RuntimeError):
    """A state transition that the design forbids."""


class UnknownJob(StoreError):
    pass


class CaptureConflict(StoreError):
    """The same sequence number arrived twice with different content."""


@dataclass(frozen=True)
class Capture:
    job_id: str
    seq: int
    capture_id: str
    image_sha256: str
    bytes_written: int
    path: Path
    committed_at: datetime
    expire_at: datetime
    read_at: datetime | None = None
    discarded: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class Store:
    """One SQLite database and one page-store root."""

    def __init__(self, db_path: Path, page_store: Path) -> None:
        self.db_path = Path(db_path)
        self.page_store = Path(page_store)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.page_store.mkdir(parents=True, exist_ok=True)
        os.chmod(self.page_store, 0o700)
        self._create()

    # -- schema --------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _create(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    asin TEXT NOT NULL,
                    dpr INTEGER NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    title TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    bundle_sha256 TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS captures (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    capture_id TEXT NOT NULL,
                    image_sha256 TEXT NOT NULL,
                    bytes_written INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    expire_at TEXT NOT NULL,
                    read_at TEXT,
                    discarded INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (job_id, seq)
                );
                CREATE TABLE IF NOT EXISTS transitions (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    at TEXT NOT NULL,
                    note TEXT,
                    PRIMARY KEY (job_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS recapture (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    dpr INTEGER NOT NULL,
                    requested_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, position)
                );
                """
            )

    # -- jobs ----------------------------------------------------------------

    def create_job(
        self, job_id: str, asin: str, dpr: int, pipeline_version: str, title: str | None = None
    ) -> None:
        moment = _iso(_now())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO jobs (job_id, asin, dpr, pipeline_version, title, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (job_id, asin, dpr, pipeline_version, title, STATES[0], moment, moment),
            )
            db.execute(
                "INSERT INTO transitions (job_id, ordinal, state, at, note) VALUES (?,?,?,?,?)",
                (job_id, 0, STATES[0], moment, "job created"),
            )
            db.execute("COMMIT")

    def job(self, job_id: str) -> sqlite3.Row:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob(job_id)
        return row

    def job_ids(self) -> tuple[str, ...]:
        with self._connect() as db:
            rows = db.execute("SELECT job_id FROM jobs ORDER BY created_at").fetchall()
        return tuple(row["job_id"] for row in rows)

    def state_of(self, job_id: str) -> State:
        return self.job(job_id)["state"]  # type: ignore[return-value]

    def transitions(self, job_id: str) -> tuple[str, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT state FROM transitions WHERE job_id = ? ORDER BY ordinal", (job_id,)
            ).fetchall()
        return tuple(row["state"] for row in rows)

    def advance(self, job_id: str, state: State, note: str = "") -> None:
        """Record the next step, refusing any that the design does not allow.

        `final` requires that the images are already gone: the step before it is
        `images_deleted`, and the deletion is verified against the page store, not
        taken on trust.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise UnknownJob(job_id)
            current = row["state"]
            allowed = STATES[STATES.index(current) + 1] if STATES.index(current) + 1 < len(STATES) else None
            if state != allowed:
                db.execute("ROLLBACK")
                raise StoreError(f"{job_id}: {current} -> {state} is not the next step")
            # The state has to be true when it is recorded. `images_deleted` means
            # the images are gone, verified against the page store, and `final`
            # inherits that: a crash between deleting and recording leaves the job
            # at bundle_written with nothing on disk, which re-deleting handles.
            if state in {"images_deleted", "final"} and self._images_on_disk(job_id):
                db.execute("ROLLBACK")
                raise StoreError(f"{job_id}: {state} is not allowed while images remain")
            ordinal = db.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 AS n FROM transitions WHERE job_id = ?", (job_id,)
            ).fetchone()["n"]
            moment = _iso(_now())
            db.execute(
                "INSERT INTO transitions (job_id, ordinal, state, at, note) VALUES (?,?,?,?,?)",
                (job_id, ordinal, state, moment, note),
            )
            db.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?", (state, moment, job_id)
            )
            db.execute("COMMIT")

    def fail(self, job_id: str, error: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET error = ?, updated_at = ? WHERE job_id = ?", (error, _iso(_now()), job_id)
            )

    def set_bundle_hash(self, job_id: str, sha: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET bundle_sha256 = ?, updated_at = ? WHERE job_id = ?",
                (sha, _iso(_now()), job_id),
            )

    def invalidate_for_pipeline_version(self, old: str, new: str) -> tuple[str, ...]:
        """A version change invalidates every non-final book's work.

        The rows stay: what a reader produced under the old rules is evidence
        about that reader. What changes is the job's state, which drops back to
        `captured` so the pages are read again under the new rules.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT job_id, state FROM jobs WHERE pipeline_version = ? AND state != 'final'", (old,)
            ).fetchall()
            moment = _iso(_now())
            for row in rows:
                db.execute(
                    "UPDATE jobs SET state = 'captured', pipeline_version = ?, updated_at = ?,"
                    " bundle_sha256 = NULL WHERE job_id = ?",
                    (new, moment, row["job_id"]),
                )
                db.execute("DELETE FROM captures WHERE job_id = ?", (row["job_id"],))
            db.execute("COMMIT")
        return tuple(row["job_id"] for row in rows)

    # -- captures ------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        """The owner-only directory for one job's images."""
        directory = self.page_store / job_id
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        return directory

    def commit_capture(
        self,
        job_id: str,
        seq: int,
        capture_id: str,
        image_sha256: str,
        image_bytes: bytes,
        *,
        ttl_days: int = IMAGE_TTL_DAYS,
    ) -> Capture:
        """Write the image durably, then record it. In that order, always.

        The file is fsynced and its directory fsynced before the row exists, so a
        `CommitAck` is never sent for an image that a power cut can lose.
        """
        self.job(job_id)
        directory = self.job_dir(job_id)
        path = directory / f"{seq:06d}-{capture_id}.png"
        temporary = path.with_suffix(".png.part")
        with temporary.open("wb") as handle:
            handle.write(image_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        moment = _now()
        expire_at = moment + timedelta(days=ttl_days)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT capture_id, image_sha256, discarded, path FROM captures WHERE job_id = ? AND seq = ?",
                (job_id, seq),
            ).fetchone()
            same_content = existing is not None and (
                existing["capture_id"] == capture_id and existing["image_sha256"] == image_sha256
            )
            if existing is not None and not same_content:
                # A re-capture of the same page is not a conflict: design 6 has the
                # sweeper expire an unfinished page's image and the walk take it
                # again under a new capture_id. What must not happen is *two* live
                # captures claiming one sequence, so the old row is only replaced
                # when it is already discarded or its image is already gone.
                stale = bool(existing["discarded"]) or not Path(existing["path"]).exists()
                if not stale:
                    db.execute("ROLLBACK")
                    path.unlink(missing_ok=True)
                    raise CaptureConflict(f"{job_id} seq {seq} already committed with other content")
                db.execute("DELETE FROM captures WHERE job_id = ? AND seq = ?", (job_id, seq))
            if existing is None or not same_content:
                db.execute(
                    "INSERT INTO captures (job_id, seq, capture_id, image_sha256, bytes_written,"
                    " path, committed_at, expire_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        seq,
                        capture_id,
                        image_sha256,
                        len(image_bytes),
                        str(path),
                        _iso(moment),
                        _iso(expire_at),
                    ),
                )
            db.execute("UPDATE jobs SET updated_at = ? WHERE job_id = ?", (_iso(moment), job_id))
            db.execute("COMMIT")
        return Capture(
            job_id=job_id,
            seq=seq,
            capture_id=capture_id,
            image_sha256=image_sha256,
            bytes_written=len(image_bytes),
            path=path,
            committed_at=moment,
            expire_at=expire_at,
        )

    def captures(self, job_id: str, *, include_discarded: bool = False) -> tuple[Capture, ...]:
        query = "SELECT * FROM captures WHERE job_id = ?"
        if not include_discarded:
            query += " AND discarded = 0"
        query += " ORDER BY seq"
        with self._connect() as db:
            rows = db.execute(query, (job_id,)).fetchall()
        return tuple(
            Capture(
                job_id=row["job_id"],
                seq=row["seq"],
                capture_id=row["capture_id"],
                image_sha256=row["image_sha256"],
                bytes_written=row["bytes_written"],
                path=Path(row["path"]),
                committed_at=datetime.fromisoformat(row["committed_at"]),
                expire_at=datetime.fromisoformat(row["expire_at"]),
                read_at=datetime.fromisoformat(row["read_at"]) if row["read_at"] else None,
                discarded=bool(row["discarded"]),
            )
            for row in rows
        )

    def last_committed_seq(self, job_id: str) -> int | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(seq) AS seq FROM captures WHERE job_id = ? AND discarded = 0", (job_id,)
            ).fetchone()
        return row["seq"]

    def discard_captures_for(self, job_id: str, capture_id: str) -> int:
        """Results bound to a deleted image are discarded, not kept (design 6)."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE captures SET discarded = 1 WHERE job_id = ? AND capture_id = ? AND discarded = 0",
                (job_id, capture_id),
            )
        return cursor.rowcount

    def request_recapture(self, job_id: str, position: int, reason: str, dpr: int) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO recapture (job_id, position, reason, dpr, requested_at)"
                " VALUES (?,?,?,?,?)",
                (job_id, position, reason, dpr, _iso(_now())),
            )

    def recapture_requests(self, job_id: str) -> tuple[tuple[int, str, int], ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT position, reason, dpr FROM recapture WHERE job_id = ? ORDER BY position",
                (job_id,),
            ).fetchall()
        return tuple((row["position"], row["reason"], row["dpr"]) for row in rows)

    def forget_recapture(self, job_id: str, position: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM recapture WHERE job_id = ? AND position = ?", (job_id, position))

    # -- images --------------------------------------------------------------

    def _images_on_disk(self, job_id: str) -> bool:
        directory = self.page_store / job_id
        if not directory.exists():
            return False
        return any(child.is_file() for child in directory.iterdir())

    def delete_images(self, job_id: str) -> int:
        """Delete a job's images and say how many went.

        Deletion is the step `final` depends on, so it is verified rather than
        assumed: the directory is removed and re-checked, and the captures whose
        image is gone are marked discarded so that results bound to them cannot
        be used again.
        """
        directory = self.page_store / job_id
        deleted = 0
        if directory.exists():
            deleted = sum(1 for child in directory.iterdir() if child.is_file())
            shutil.rmtree(directory)
        if directory.exists():
            raise StoreError(f"{job_id}: the page directory survived deletion")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE captures SET discarded = 1 WHERE job_id = ?", (job_id,))
            db.execute("COMMIT")
        return deleted

    # -- sweeper support -----------------------------------------------------

    def expired_captures(self, moment: datetime | None = None) -> tuple[Capture, ...]:
        """Every committed capture past the hard cap, across every job."""
        moment = moment or _now()
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM captures WHERE expire_at <= ? AND discarded = 0 ORDER BY expire_at",
                (_iso(moment),),
            ).fetchall()
        return tuple(
            Capture(
                job_id=row["job_id"],
                seq=row["seq"],
                capture_id=row["capture_id"],
                image_sha256=row["image_sha256"],
                bytes_written=row["bytes_written"],
                path=Path(row["path"]),
                committed_at=datetime.fromisoformat(row["committed_at"]),
                expire_at=datetime.fromisoformat(row["expire_at"]),
                read_at=datetime.fromisoformat(row["read_at"]) if row["read_at"] else None,
                discarded=bool(row["discarded"]),
            )
            for row in rows
        )

    def forget_capture(self, job_id: str, seq: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM captures WHERE job_id = ? AND seq = ?", (job_id, seq))
