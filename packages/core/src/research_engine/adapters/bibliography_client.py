"""Adapter that conforms the core provenance repositories to the SDK
``BibliographyClient`` Protocol.

Plugins with the ``ingest`` permission receive an instance as their
``bibliography`` client. With ``IngestionClient.ingest_drafts`` it is the whole
of what a loader needs to build a corpus deliberately: declare an edition, say
how it was got (or why it cannot be), record the raw bytes by digest, and report
coverage against the source's known shape. Every value is the loader's
vocabulary. Who holds an edition authoritative is a profile's business
(`ProfileClient`), not the edition's.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from research_engine.domain.acquisitions import (
    AcquisitionDraft,
    AcquisitionStatus,
)
from research_engine.domain.errors import NotFoundError

if TYPE_CHECKING:
    from research_engine.adapters.storage.postgres.repositories import (
        PGAcquisitionRepo,
        PGEditionRepo,
    )
    from research_engine.domain.acquisitions import Acquisition


class BibliographyServiceAdapter:
    """Concrete implementation of the SDK ``BibliographyClient`` Protocol."""

    def __init__(
        self,
        *,
        editions: PGEditionRepo,
        acquisitions: PGAcquisitionRepo,
        transaction_factory: Any,
        data_dir: Path,
    ) -> None:
        self._editions = editions
        self._acquisitions = acquisitions
        self._tx_factory = transaction_factory
        self._data_dir = data_dir.resolve()

    async def register_source(
        self,
        edition_key: str,
        *,
        csl: dict[str, Any] | None = None,
        work_key: str | None = None,
        collection: str | None = None,
        status: str | None = None,
        method: str | None = None,
        url: str | None = None,
        license: str | None = None,  # noqa: A002 - the column's name
        license_notes: str | None = None,
        retrieved_at: datetime | str | None = None,
        blocked_reason: str | None = None,
        suggested_acquisition: str | None = None,
        manifest_ref: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Declare an edition and how it is (to be) got.

        Idempotent, and partial: a re-registration overwrites only the fields
        it passes, so saying one thing never erases another. Without
        ``status`` it also leaves the source's progress alone, so re-reading a
        manifest never sends a loaded source back to `planned`. ``work_key``
        names the work this is an edition of, so editions of one work can be
        read once and carried across.
        """
        given = {
            "collection": collection,
            "method": method,
            "url": url,
            "license": license,
            "license_notes": license_notes,
            "suggested_acquisition": suggested_acquisition,
            "manifest_ref": manifest_ref,
            "attributes": attributes,
        }
        declared = frozenset(k for k, v in given.items() if v is not None)
        draft = AcquisitionDraft(
            collection=collection or "main",
            status=status or AcquisitionStatus.planned,
            method=method,
            url=url,
            license=license,
            license_notes=license_notes,
            retrieved_at=_when(retrieved_at),
            blocked_reason=blocked_reason,
            suggested_acquisition=suggested_acquisition,
            manifest_ref=manifest_ref,
            attributes=attributes or {},
        )
        async with self._tx_factory() as tx:
            edition = await self._editions.upsert_key(
                tx, edition_key, csl, work_key=work_key
            )
            acquisition = await self._acquisitions.upsert(
                tx, edition.id, draft, set_status=status is not None,
                declared=declared,
            )
        return _dump(acquisition)

    async def mark_blocked(
        self,
        edition_key: str,
        *,
        reason: str,
        suggested_acquisition: str,
        **declared: Any,
    ) -> dict[str, Any]:
        """Record that an edition cannot be had, why, and how it could be."""
        return await self.register_source(
            edition_key,
            status=AcquisitionStatus.blocked,
            blocked_reason=reason,
            suggested_acquisition=suggested_acquisition,
            **declared,
        )

    async def set_status(
        self,
        edition_key: str,
        status: str,
        *,
        retrieved_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Move a registered edition to `acquired`, `loaded` or `failed`."""
        state = AcquisitionStatus(status)
        if state is AcquisitionStatus.blocked:
            raise ValueError("use mark_blocked to block a source: it needs a reason")
        acquisition = await self._require(edition_key)
        async with self._tx_factory() as tx:
            updated = await self._acquisitions.set_status(
                tx, acquisition.id, state.value, retrieved_at=_when(retrieved_at)
            )
        return _dump(updated)

    async def attach_artifact(
        self,
        edition_key: str,
        path: str | Path,
        *,
        sha256: str | None = None,
        url: str | None = None,
        media_type: str | None = None,
        retrieved_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Record a raw file by digest. The file must exist and match ``sha256``.

        The digest is computed here, not trusted: a loader that passes one is
        checked against the bytes on disk. Paths under the engine's data
        directory are stored relative to it, so the corpus survives the
        directory moving.
        """
        file = Path(path).expanduser().resolve()
        if not file.is_file():
            raise FileNotFoundError(f"artifact {file} does not exist")
        digest = _sha256(file)
        if sha256 is not None and sha256.lower() != digest.hex():
            raise ValueError(
                f"artifact {file} has sha256 {digest.hex()}, not the {sha256} given"
            )
        acquisition = await self._require(edition_key)
        async with self._tx_factory() as tx:
            artifact = await self._acquisitions.add_artifact(
                tx,
                acquisition.id,
                sha256=digest,
                path=self._stored_path(file),
                size=file.stat().st_size,
                url=url,
                media_type=media_type,
                retrieved_at=_when(retrieved_at),
            )
        return artifact.model_dump(mode="json")

    async def report_coverage(
        self,
        edition_key: str,
        *,
        unit: str,
        loaded_units: int,
        checker: str,
        checker_version: str,
        expected_units: int | None = None,
        anomalies: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Record loaded units against the expected count for one unit kind.

        `ok` is computed, not passed: the count matches (or nothing was
        expected) and no anomaly was reported.
        """
        anomalies = list(anomalies or [])
        ok = (expected_units is None or loaded_units == expected_units) and not anomalies
        acquisition = await self._require(edition_key)
        async with self._tx_factory() as tx:
            report = await self._acquisitions.add_coverage(
                tx,
                acquisition.id,
                unit=unit,
                loaded_units=loaded_units,
                expected_units=expected_units,
                anomalies=anomalies,
                ok=ok,
                checker=checker,
                checker_version=checker_version,
            )
        return report.model_dump(mode="json")

    async def get_source(self, edition_key: str) -> dict[str, Any] | None:
        """One edition's acquisition, with its artifacts and coverage history."""
        acquisition = await self._acquisitions.get_by_edition_key(edition_key)
        if acquisition is None:
            return None
        result = _dump(acquisition)
        edition = await self._editions.get(acquisition.edition_id)
        result["work_key"] = edition.work_key if edition else None
        result["artifacts"] = [
            a.model_dump(mode="json")
            for a in await self._acquisitions.artifacts_for(acquisition.id)
        ]
        result["coverage"] = [
            c.model_dump(mode="json")
            for c in await self._acquisitions.coverage_for(acquisition.id)
        ]
        return result

    async def list_sources(
        self, *, status: str | None = None, collection: str | None = None
    ) -> list[dict[str, Any]]:
        if status is not None:
            status = AcquisitionStatus(status).value
        return [
            _dump(a)
            for a in await self._acquisitions.list(status=status, collection=collection)
        ]

    async def _require(self, edition_key: str) -> Acquisition:
        acquisition = await self._acquisitions.get_by_edition_key(edition_key)
        if acquisition is None:
            # Register first: an edition with no acquisition has no lifecycle.
            raise NotFoundError("acquisition for edition", edition_key)
        return acquisition

    def _stored_path(self, file: Path) -> str:
        try:
            return str(file.relative_to(self._data_dir))
        except ValueError:
            return str(file)


def _dump(acquisition: Acquisition) -> dict[str, Any]:
    return acquisition.model_dump(mode="json")


def _sha256(file: Path) -> bytes:
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.digest()


def _when(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
