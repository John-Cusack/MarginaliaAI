"""The LLM budget guard."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from research_engine.adapters.clock import FakeClock
from research_engine.adapters.llm.budget_guard import BudgetGuard
from research_engine.domain.provenance import BudgetExceeded

pytestmark = pytest.mark.unit


class FakeLLM:
    def __init__(self) -> None:
        self.completions: list[str] = []
        self.structured_calls: list[str] = []
        self.model_name = "fake-model"

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        caller: str = "core",
        purpose: str = "general",
        **kwargs: Any,
    ) -> tuple[str, UUID]:
        self.completions.append(purpose)
        return "ok", uuid4()

    async def structured(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        model: str | None = None,
        caller: str = "core",
        purpose: str = "extraction",
        **kwargs: Any,
    ) -> tuple[dict[str, Any], UUID]:
        self.structured_calls.append(purpose)
        return {}, uuid4()


class FakeCallLog:
    def __init__(self, cost: float = 0.0) -> None:
        self.cost = cost
        self.queries = 0

    async def total_cost_since(self, since: datetime) -> float:
        self.queries += 1
        return self.cost


def build(cost: float, limit: float = 10.0, recheck: float = 30.0) -> tuple[BudgetGuard, FakeLLM, FakeCallLog]:
    inner, log = FakeLLM(), FakeCallLog(cost)
    guard = BudgetGuard(
        inner,
        log,
        FakeClock(datetime(2026, 8, 10, tzinfo=UTC)),
        limit_usd=limit,
        recheck_seconds=recheck,
    )
    return guard, inner, log


async def test_calls_pass_through_under_budget() -> None:
    guard, inner, _ = build(cost=3.0)
    text, _ = await guard.complete([{"role": "user", "content": "hi"}])
    assert text == "ok"
    assert inner.completions == ["general"]


async def test_structured_calls_are_guarded_too() -> None:
    """Extraction is the expensive path; guarding only `complete` would miss it."""
    guard, inner, _ = build(cost=50.0)
    with pytest.raises(BudgetExceeded):
        await guard.structured([{"role": "user", "content": "hi"}], {})
    assert inner.structured_calls == []


async def test_over_budget_refuses_without_calling_the_provider() -> None:
    guard, inner, _ = build(cost=10.5)
    with pytest.raises(BudgetExceeded) as exc:
        await guard.complete([{"role": "user", "content": "hi"}])
    assert inner.completions == []
    assert exc.value.spent == 10.5
    assert exc.value.limit == 10.0


async def test_exactly_at_limit_refuses() -> None:
    guard, inner, _ = build(cost=10.0)
    with pytest.raises(BudgetExceeded):
        await guard.complete([{"role": "user", "content": "hi"}])
    assert inner.completions == []


async def test_spend_is_not_requeried_per_call() -> None:
    """A batch extraction issues thousands of calls; one aggregate each would
    cost more than the guard saves."""
    guard, inner, log = build(cost=1.0)
    for _ in range(50):
        await guard.complete([{"role": "user", "content": "hi"}])
    assert log.queries == 1
    assert len(inner.completions) == 50


async def test_cache_expiry_refreshes_spend() -> None:
    guard, inner, log = build(cost=1.0, recheck=0.0)
    await guard.complete([{"role": "user", "content": "hi"}])
    log.cost = 99.0
    with pytest.raises(BudgetExceeded):
        await guard.complete([{"role": "user", "content": "hi"}])
    assert len(inner.completions) == 1


async def test_unknown_attributes_pass_through_to_the_inner_adapter() -> None:
    guard, _, _ = build(cost=0.0)
    assert guard.model_name == "fake-model"


async def test_error_message_names_the_setting_to_change() -> None:
    guard, _, _ = build(cost=12.0)
    with pytest.raises(BudgetExceeded, match="RE_LLM_BUDGET_USD"):
        await guard.complete([{"role": "user", "content": "hi"}])
