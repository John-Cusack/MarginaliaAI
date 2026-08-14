"""Reading back what `cost_estimate` has been recording all along."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from research_engine.adapters.storage.postgres.repositories.provenance import (
    PGLLMCallLogRepo,
)
from research_engine.adapters.storage.postgres.schema import llm_calls
from research_engine.domain.provenance import LLMCallDraft

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def calls(engine: AsyncEngine):
    """Log a handful of calls, then remove exactly those."""
    repo = PGLLMCallLogRepo(engine)
    made = []
    for purpose, caller, model, cost, status in [
        ("extraction", "core", "sonnet", 1.50, "ok"),
        ("extraction", "core", "sonnet", 2.25, "ok"),
        ("extraction", "acad", "haiku", 0.10, "error"),
        ("entity_resolution", "core", "sonnet", 0.40, "ok"),
    ]:
        made.append(
            await repo.insert(
                LLMCallDraft(
                    purpose=purpose,
                    caller=caller,
                    model=model,
                    input_tokens=1000,
                    output_tokens=200,
                    cost_estimate=cost,
                    duration_ms=100,
                    status=status,
                )
            )
        )
    try:
        yield repo, made
    finally:
        async with engine.begin() as conn:
            await conn.execute(llm_calls.delete().where(llm_calls.c.id.in_([c.id for c in made])))


async def test_groups_spend_by_purpose_caller_and_model(calls) -> None:
    repo, made = calls
    since = min(c.created_at for c in made) - timedelta(seconds=1)

    summary = await repo.usage_summary(since=since)

    assert summary.total_calls == 4
    assert summary.total_cost == pytest.approx(4.25)

    extraction_core = next(
        g
        for g in summary.groups
        if g.key == {"purpose": "extraction", "caller": "core", "model": "sonnet"}
    )
    assert extraction_core.calls == 2
    assert extraction_core.cost == pytest.approx(3.75)
    assert extraction_core.input_tokens == 2000
    assert extraction_core.errors == 0


async def test_groups_are_ordered_by_spend(calls) -> None:
    repo, made = calls
    since = min(c.created_at for c in made) - timedelta(seconds=1)
    summary = await repo.usage_summary(since=since)
    costs = [g.cost for g in summary.groups]
    assert costs == sorted(costs, reverse=True)


async def test_failed_calls_are_counted_separately(calls) -> None:
    repo, made = calls
    since = min(c.created_at for c in made) - timedelta(seconds=1)
    summary = await repo.usage_summary(since=since)
    acad = next(g for g in summary.groups if g.key["caller"] == "acad")
    assert acad.errors == 1


async def test_window_excludes_older_calls(calls) -> None:
    repo, _ = calls
    summary = await repo.usage_summary(since=datetime.now(UTC) + timedelta(days=1))
    assert summary.total_calls == 0
    assert summary.total_cost == 0


async def test_regrouping_by_a_single_column(calls) -> None:
    repo, made = calls
    since = min(c.created_at for c in made) - timedelta(seconds=1)
    summary = await repo.usage_summary(since=since, group_by=("model",))
    by_model = {g.key["model"]: g.cost for g in summary.groups}
    assert by_model["sonnet"] == pytest.approx(4.15)
    assert by_model["haiku"] == pytest.approx(0.10)


async def test_unknown_group_column_is_refused(calls) -> None:
    repo, _ = calls
    with pytest.raises(ValueError, match="groupable columns"):
        await repo.usage_summary(group_by=("purpose", "id; DROP TABLE core.llm_calls"))


async def test_total_cost_since_matches_the_summary(calls) -> None:
    repo, made = calls
    since = min(c.created_at for c in made) - timedelta(seconds=1)
    total = await repo.total_cost_since(since)
    summary = await repo.usage_summary(since=since)
    assert total == pytest.approx(summary.total_cost)


async def test_empty_window_returns_zero_not_null(engine: AsyncEngine) -> None:
    """COALESCE matters: SUM over no rows is NULL, and float(None) raises."""
    repo = PGLLMCallLogRepo(engine)
    assert await repo.total_cost_since(datetime.now(UTC) + timedelta(days=365)) == 0.0
