"""Recover canonical text for documents ingested before `document_texts` existed.

Passage offsets address a document's canonical text. Documents ingested before
that table existed have none, so nothing about them can be quote-verified or
re-anchored — `reindex chunks` correctly refuses them rather than guessing.

Recovery means re-parsing the source, which is only possible when the source is
still reachable. Three outcomes, and the distinction matters because they cost
wildly different amounts:

* **fast** — the source is a file a lightweight parser handles (plain text,
  markdown, HTML). Seconds.
* **slow** — the source is a file that needs docling. Minutes to hours for a
  large scan.
* **unreachable** — the source is not a file at all (a `logos:` batch URI), or
  the file is gone. Only the pack that fetched it can produce the text.

Recovering the text is deliberately *not* the same operation as re-anchoring the
passages onto it. This stores the substrate; `reindex chunks` then re-anchors,
and its orphan report is what tells you whether the recovered text actually
matches what the old passages were cut from.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.adapters.storage.postgres.schema import document_texts, documents

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()

#: Modules heavy enough that a backfill run over them should be a deliberate act.
SLOW_MODULES = frozenset({"docling"})


class Route(enum.StrEnum):
    FAST = "fast"
    SLOW = "slow"
    UNREACHABLE = "unreachable"
    MISSING_FILE = "missing_file"
    ALREADY_PRESENT = "already_present"


@dataclass
class Candidate:
    document_id: UUID
    title: str | None
    source: str
    document_type: str
    parser: str
    route: Route
    module_id: str | None = None
    size_bytes: int | None = None
    detail: str = ""


@dataclass
class TextBackfillReport:
    dry_run: bool = False
    candidates: list[Candidate] = field(default_factory=list)
    recovered: int = 0
    failed: dict[str, str] = field(default_factory=dict)

    def by_route(self) -> dict[Route, list[Candidate]]:
        grouped: dict[Route, list[Candidate]] = {}
        for candidate in self.candidates:
            grouped.setdefault(candidate.route, []).append(candidate)
        return grouped


class TextBackfillService:
    """Recovers canonical text for documents that have none."""

    def __init__(self, engine: AsyncEngine, document_text_repo: Any, dispatcher: Any) -> None:
        self._engine = engine
        self._texts = document_text_repo
        self._dispatcher = dispatcher

    async def plan(
        self, document_ids: Sequence[UUID] | None = None
    ) -> list[Candidate]:
        """Classify every document lacking canonical text by recovery route."""
        stmt = (
            sa.select(
                documents.c.id,
                documents.c.title,
                documents.c.source,
                documents.c.document_type,
                documents.c.parser,
            )
            .where(
                ~sa.exists(
                    sa.select(sa.literal(1)).where(
                        document_texts.c.document_id == documents.c.id
                    )
                )
            )
            .order_by(documents.c.document_type, documents.c.source)
        )
        if document_ids is not None:
            stmt = stmt.where(documents.c.id.in_(list(document_ids)))

        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()

        return [await self._classify(row) for row in rows]

    async def _classify(self, row: Any) -> Candidate:
        candidate = Candidate(
            document_id=row.id,
            title=row.title,
            source=row.source,
            document_type=row.document_type,
            parser=row.parser,
            route=Route.UNREACHABLE,
        )

        # A source that is not a filesystem path — `logos:LLS:...:batch:b0000` —
        # can only be re-fetched by the pack that produced it.
        if "://" in row.source or (":" in row.source and not row.source.startswith("/")):
            candidate.detail = "source is a pack URI; re-run that pack's ingest"
            return candidate

        path = Path(row.source)
        if not path.is_file():
            candidate.route = Route.MISSING_FILE
            candidate.detail = "source file no longer exists"
            return candidate

        candidate.size_bytes = path.stat().st_size
        try:
            module = await self._dispatcher.dispatch(path)
        except Exception as exc:  # noqa: BLE001 - no module means unreachable, not a crash
            candidate.detail = f"no ingestion module accepts this source: {exc}"
            return candidate

        candidate.module_id = module.id
        candidate.route = Route.SLOW if module.id in SLOW_MODULES else Route.FAST
        return candidate

    async def recover(
        self,
        document_ids: Sequence[UUID] | None = None,
        *,
        routes: set[Route] | None = None,
        dry_run: bool = False,
    ) -> TextBackfillReport:
        """Re-parse recoverable sources and store the result as canonical text.

        *routes* selects which tiers to act on; by default only FAST, because a
        docling pass over a 700 MB scan should be something you ask for.
        """
        routes = routes or {Route.FAST}
        report = TextBackfillReport(dry_run=dry_run)
        report.candidates = await self.plan(document_ids)

        if dry_run:
            return report

        for candidate in report.candidates:
            if candidate.route not in routes:
                continue
            try:
                await self._recover_one(candidate)
                report.recovered += 1
            except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
                logger.error(
                    "text_backfill_failed",
                    document_id=str(candidate.document_id),
                    source=candidate.source,
                    error=str(exc),
                )
                report.failed[str(candidate.document_id)] = str(exc)

        return report

    async def _recover_one(self, candidate: Candidate) -> None:
        path = Path(candidate.source)
        module = await self._dispatcher.dispatch(path)
        full_text, _title, _metadata = await module.parse(path)
        if not full_text or not full_text.strip():
            raise ValueError("parser produced no text")

        async with transaction(self._engine) as tx:
            await self._texts.put(
                tx, candidate.document_id, full_text, module.id, module.version
            )
        logger.info(
            "text_backfilled",
            document_id=str(candidate.document_id),
            module=module.id,
            chars=len(full_text),
        )
