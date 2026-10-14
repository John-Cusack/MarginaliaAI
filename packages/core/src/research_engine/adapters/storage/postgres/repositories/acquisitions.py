"""Acquisitions — how each edition was got, its raw bytes, and its coverage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import (
    acquisitions,
    artifacts,
    coverage_reports,
    editions,
)
from research_engine.domain.acquisitions import (
    Acquisition,
    AcquisitionDraft,
    Artifact,
    CoverageReport,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.ports.repositories import Transaction

#: Columns a re-registration may overwrite without saying anything about
#: progress. Re-reading a manifest must not knock a loaded source back to
#: `planned`, so the lifecycle columns move only when the caller asks.
_DECLARED = (
    "collection",
    "method",
    "url",
    "license",
    "license_notes",
    "suggested_acquisition",
    "manifest_ref",
    "attributes",
)
_LIFECYCLE = ("status", "blocked_reason", "retrieved_at")


class PGAcquisitionRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(
        self,
        tx: Transaction,
        edition_id: UUID,
        draft: AcquisitionDraft,
        *,
        set_status: bool,
        declared: frozenset[str] | None = None,
    ) -> Acquisition:
        """Create the edition's acquisition, or update what it declares.

        On an existing row only the ``declared`` columns are overwritten (all
        of them when None), so a caller saying one thing — a new licence — does
        not erase what it did not mention. With ``set_status`` false the row
        also keeps its status, blocked reason and retrieval time; a new row
        takes the draft's.
        """
        values = {
            "id": uuid7(),
            "edition_id": edition_id,
            **{col: _plain(getattr(draft, col)) for col in _DECLARED + _LIFECYCLE},
        }
        stmt = pg_insert(acquisitions).values(**values)
        overwritten = [c for c in _DECLARED if declared is None or c in declared]
        if set_status:
            # A status change without a retrieval time keeps the one recorded.
            overwritten += [
                c for c in _LIFECYCLE if c != "retrieved_at" or draft.retrieved_at
            ]
        stmt = stmt.on_conflict_do_update(
            index_elements=[acquisitions.c.edition_id],
            set_={col: stmt.excluded[col] for col in overwritten}
            | {"updated_at": sa.func.now()},
        )
        await tx.conn.execute(stmt)
        row = (
            await tx.conn.execute(
                _joined().where(acquisitions.c.edition_id == edition_id)
            )
        ).one()
        return self._to_domain(row)

    async def set_status(
        self,
        tx: Transaction,
        acquisition_id: UUID,
        status: str,
        *,
        blocked_reason: str | None = None,
        suggested_acquisition: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> Acquisition:
        """Move one acquisition along its lifecycle.

        A status other than `blocked` clears the blocked reason, so a source
        that has since been got stops appearing in the queue.
        """
        values: dict[str, Any] = {
            "status": status,
            "blocked_reason": blocked_reason if status == "blocked" else None,
            "updated_at": sa.func.now(),
        }
        if suggested_acquisition is not None:
            values["suggested_acquisition"] = suggested_acquisition
        if retrieved_at is not None:
            values["retrieved_at"] = retrieved_at
        await tx.conn.execute(
            acquisitions.update()
            .where(acquisitions.c.id == acquisition_id)
            .values(**values)
        )
        row = (
            await tx.conn.execute(_joined().where(acquisitions.c.id == acquisition_id))
        ).one()
        return self._to_domain(row)

    async def get_by_edition_key(self, edition_key: str) -> Acquisition | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(_joined().where(editions.c.edition_key == edition_key))
            ).first()
        return self._to_domain(row) if row else None

    async def list(
        self, *, status: str | None = None, collection: str | None = None
    ) -> list[Acquisition]:
        query = _joined().order_by(editions.c.edition_key)
        if status is not None:
            query = query.where(acquisitions.c.status == status)
        if collection is not None:
            query = query.where(acquisitions.c.collection == collection)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [self._to_domain(row) for row in rows]

    async def add_artifact(
        self,
        tx: Transaction,
        acquisition_id: UUID,
        *,
        sha256: bytes,
        path: str,
        size: int,
        url: str | None = None,
        media_type: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> Artifact:
        """Record one raw file. The same bytes recorded twice are one row."""
        values: dict[str, Any] = {
            "id": uuid7(),
            "acquisition_id": acquisition_id,
            "sha256": sha256,
            "path": path,
            "bytes": size,
            "url": url,
            "media_type": media_type,
        }
        if retrieved_at is not None:
            values["retrieved_at"] = retrieved_at
        await tx.conn.execute(
            pg_insert(artifacts)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[artifacts.c.acquisition_id, artifacts.c.sha256]
            )
        )
        row = (
            await tx.conn.execute(
                artifacts.select().where(
                    artifacts.c.acquisition_id == acquisition_id,
                    artifacts.c.sha256 == sha256,
                )
            )
        ).one()
        return _artifact(row)

    async def artifacts_for(self, acquisition_id: UUID) -> list[Artifact]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    artifacts.select()
                    .where(artifacts.c.acquisition_id == acquisition_id)
                    .order_by(artifacts.c.retrieved_at)
                )
            ).all()
        return [_artifact(row) for row in rows]

    async def add_coverage(
        self,
        tx: Transaction,
        acquisition_id: UUID,
        *,
        unit: str,
        loaded_units: int,
        expected_units: int | None,
        anomalies: list[Any],
        ok: bool,
        checker: str,
        checker_version: str,
    ) -> CoverageReport:
        row = (
            await tx.conn.execute(
                coverage_reports.insert()
                .values(
                    id=uuid7(),
                    acquisition_id=acquisition_id,
                    unit=unit,
                    expected_units=expected_units,
                    loaded_units=loaded_units,
                    anomalies=anomalies,
                    ok=ok,
                    checker=checker,
                    checker_version=checker_version,
                )
                .returning(coverage_reports)
            )
        ).one()
        return _coverage(row)

    async def coverage_for(self, acquisition_id: UUID) -> list[CoverageReport]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    coverage_reports.select()
                    .where(coverage_reports.c.acquisition_id == acquisition_id)
                    .order_by(coverage_reports.c.created_at)
                )
            ).all()
        return [_coverage(row) for row in rows]

    @staticmethod
    def _to_domain(row: Any) -> Acquisition:
        return Acquisition(
            id=row.id,
            edition_id=row.edition_id,
            edition_key=row.edition_key,
            collection=row.collection,
            status=row.status,
            method=row.method,
            url=row.url,
            license=row.license,
            license_notes=row.license_notes,
            retrieved_at=row.retrieved_at,
            blocked_reason=row.blocked_reason,
            suggested_acquisition=row.suggested_acquisition,
            manifest_ref=row.manifest_ref,
            attributes=dict(row.attributes or {}),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _joined() -> sa.Select:
    return sa.select(acquisitions, editions.c.edition_key).join(
        editions, editions.c.id == acquisitions.c.edition_id
    )


def _plain(value: Any) -> Any:
    """Enum members as their values, so the row stores `blocked`, not the repr."""
    return getattr(value, "value", value)


def _artifact(row: Any) -> Artifact:
    return Artifact(
        id=row.id,
        acquisition_id=row.acquisition_id,
        sha256=bytes(row.sha256).hex(),
        path=row.path,
        bytes=row.bytes,
        url=row.url,
        media_type=row.media_type,
        retrieved_at=row.retrieved_at,
    )


def _coverage(row: Any) -> CoverageReport:
    return CoverageReport(
        id=row.id,
        acquisition_id=row.acquisition_id,
        unit=row.unit,
        expected_units=row.expected_units,
        loaded_units=row.loaded_units,
        anomalies=list(row.anomalies or []),
        ok=row.ok,
        checker=row.checker,
        checker_version=row.checker_version,
        created_at=row.created_at,
    )
