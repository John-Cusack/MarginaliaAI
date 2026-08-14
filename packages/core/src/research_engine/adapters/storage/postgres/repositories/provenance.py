"""Postgres provenance repositories (LLM calls and ingestion runs)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import (
    ingestion_items,
    ingestion_runs,
    llm_calls,
)
from research_engine.domain.provenance import (
    IngestionItem,
    IngestionRun,
    LLMCall,
    LLMCallDraft,
    UsageGroup,
    UsageSummary,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

#: Columns that make sense to aggregate spend by. Restricted because the names
#: are used to index into the table; nothing user-supplied reaches SQL.
_GROUPABLE_COLUMNS = frozenset({"purpose", "caller", "model", "status"})


class PGLLMCallLogRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(self, draft: LLMCallDraft) -> LLMCall:
        call_id = uuid7()
        values = {
            "id": call_id,
            "purpose": draft.purpose,
            "caller": draft.caller,
            "model": draft.model,
            "input_tokens": draft.input_tokens,
            "output_tokens": draft.output_tokens,
            "cost_estimate": draft.cost_estimate,
            "duration_ms": draft.duration_ms,
            "status": draft.status,
            "error": draft.error,
        }
        async with self._engine.begin() as conn:
            await conn.execute(llm_calls.insert().values(**values))
            return await self._get_by_id(conn, call_id)  # type: ignore[return-value]

    async def get(self, call_id: UUID) -> LLMCall | None:
        async with self._engine.connect() as conn:
            return await self._get_by_id(conn, call_id)

    async def recent(self, limit: int = 100) -> list[LLMCall]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    llm_calls.select().order_by(llm_calls.c.created_at.desc()).limit(limit)
                )
            ).all()
            return [self._to_domain(row) for row in rows]

    async def usage_summary(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        group_by: Sequence[str] = ("purpose", "caller", "model"),
    ) -> UsageSummary:
        """Aggregate spend and token counts over a time window.

        ``cost_estimate`` has been written faithfully since the schema existed
        and read by nothing. This is the read side.
        """
        unknown = [g for g in group_by if g not in _GROUPABLE_COLUMNS]
        if unknown:
            raise ValueError(
                f"Cannot group llm_calls by {unknown}; "
                f"groupable columns are {sorted(_GROUPABLE_COLUMNS)}"
            )

        group_cols = [llm_calls.c[name] for name in group_by]
        cost = sa.func.coalesce(sa.func.sum(llm_calls.c.cost_estimate), 0)
        stmt = sa.select(
            *group_cols,
            sa.func.count().label("calls"),
            sa.func.coalesce(sa.func.sum(llm_calls.c.input_tokens), 0).label("input_tokens"),
            sa.func.coalesce(sa.func.sum(llm_calls.c.output_tokens), 0).label("output_tokens"),
            cost.label("cost"),
            sa.func.count().filter(llm_calls.c.status != "ok").label("errors"),
        )
        if since is not None:
            stmt = stmt.where(llm_calls.c.created_at >= since)
        if until is not None:
            stmt = stmt.where(llm_calls.c.created_at < until)
        stmt = stmt.group_by(*group_cols).order_by(cost.desc())

        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()

        groups = [
            UsageGroup(
                key={name: getattr(row, name) for name in group_by},
                calls=row.calls,
                input_tokens=row.input_tokens or 0,
                output_tokens=row.output_tokens or 0,
                cost=float(row.cost or 0),
                errors=row.errors,
            )
            for row in rows
        ]
        return UsageSummary(
            since=since,
            until=until,
            group_by=list(group_by),
            groups=groups,
            total_calls=sum(g.calls for g in groups),
            total_cost=sum(g.cost for g in groups),
        )

    async def total_cost_since(self, since: datetime) -> float:
        """Total estimated spend since *since*. Used by the budget guard."""
        stmt = sa.select(sa.func.coalesce(sa.func.sum(llm_calls.c.cost_estimate), 0)).where(
            llm_calls.c.created_at >= since
        )
        async with self._engine.connect() as conn:
            return float((await conn.execute(stmt)).scalar_one() or 0)

    async def _get_by_id(self, conn: Any, call_id: UUID) -> LLMCall | None:
        row = (
            await conn.execute(llm_calls.select().where(llm_calls.c.id == call_id))
        ).first()
        return self._to_domain(row) if row else None

    @staticmethod
    def _to_domain(row: Any) -> LLMCall:
        return LLMCall(
            id=row.id,
            purpose=row.purpose,
            caller=row.caller,
            model=row.model,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_estimate=float(row.cost_estimate) if row.cost_estimate is not None else None,
            duration_ms=row.duration_ms,
            status=row.status,
            error=row.error,
            created_at=row.created_at,
        )


class PGIngestionRunRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def start_run(self, source_spec: dict[str, Any]) -> IngestionRun:
        run_id = uuid7()
        values = {
            "id": run_id,
            "source_spec": source_spec,
            "status": "running",
        }
        async with self._engine.begin() as conn:
            await conn.execute(ingestion_runs.insert().values(**values))
            return await self._get_run_by_id(conn, run_id)  # type: ignore[return-value]

    async def complete_run(
        self, run_id: UUID, status: str, stats: dict[str, Any]
    ) -> IngestionRun:
        async with self._engine.begin() as conn:
            await conn.execute(
                ingestion_runs.update()
                .where(ingestion_runs.c.id == run_id)
                .values(
                    status=status,
                    stats=stats,
                    completed_at=sa.func.now(),
                )
            )
            return await self._get_run_by_id(conn, run_id)  # type: ignore[return-value]

    async def add_item(
        self, run_id: UUID, source_ref: str, status: str, **kwargs: Any
    ) -> IngestionItem:
        item_id = uuid7()
        values = {
            "id": item_id,
            "run_id": run_id,
            "source_ref": source_ref,
            "status": status,
            **kwargs,
        }
        async with self._engine.begin() as conn:
            await conn.execute(ingestion_items.insert().values(**values))
            return await self._get_item_by_id(conn, item_id)  # type: ignore[return-value]

    async def update_item(self, item_id: UUID, **kwargs: Any) -> IngestionItem:
        async with self._engine.begin() as conn:
            await conn.execute(
                ingestion_items.update()
                .where(ingestion_items.c.id == item_id)
                .values(**kwargs)
            )
            return await self._get_item_by_id(conn, item_id)  # type: ignore[return-value]

    async def _get_run_by_id(self, conn: Any, run_id: UUID) -> IngestionRun | None:
        row = (
            await conn.execute(
                ingestion_runs.select().where(ingestion_runs.c.id == run_id)
            )
        ).first()
        return self._run_to_domain(row) if row else None

    async def _get_item_by_id(self, conn: Any, item_id: UUID) -> IngestionItem | None:
        row = (
            await conn.execute(
                ingestion_items.select().where(ingestion_items.c.id == item_id)
            )
        ).first()
        return self._item_to_domain(row) if row else None

    @staticmethod
    def _run_to_domain(row: Any) -> IngestionRun:
        return IngestionRun(
            id=row.id,
            started_at=row.started_at,
            completed_at=row.completed_at,
            source_spec=row.source_spec or {},
            status=row.status,
            stats=row.stats or {},
        )

    @staticmethod
    def _item_to_domain(row: Any) -> IngestionItem:
        return IngestionItem(
            id=row.id,
            run_id=row.run_id,
            source_ref=row.source_ref,
            document_id=row.document_id,
            status=row.status,
            error=row.error,
            duration_ms=row.duration_ms,
            created_at=row.created_at,
        )
