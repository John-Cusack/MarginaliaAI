"""An LLMPort wrapper that refuses calls once configured spend is exceeded.

Corpus-wide extraction is the operation that produces a surprising bill: one
command, one passage per call, tens of thousands of passages. The guard turns
that from a billing surprise into a refusal with a number attached.

It is deliberately a *port implementation*, not a check inside each adapter, so
that every LLM caller — core services and plugins alike — passes through it
without knowing it exists.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from research_engine.domain.provenance import BudgetExceeded

if TYPE_CHECKING:
    from uuid import UUID

    from research_engine.ports.clock import ClockPort
    from research_engine.ports.llm import LLMPort
    from research_engine.ports.repositories import LLMCallLogRepo

logger = structlog.get_logger()


class BudgetGuard:
    """Wraps an ``LLMPort``, refusing calls past *limit_usd* in a rolling window.

    The spend total is cached for *recheck_seconds* rather than queried per call:
    a batch extraction issues thousands of calls, and an aggregate over
    ``llm_calls`` on each one would cost more than it saves. The cost is that the
    guard can overshoot by up to one recheck interval of spend — acceptable for a
    backstop against runaway cost, and documented here so nobody mistakes it for
    a hard transactional limit.
    """

    def __init__(
        self,
        inner: LLMPort,
        llm_calls: LLMCallLogRepo,
        clock: ClockPort,
        limit_usd: float,
        window_days: int = 30,
        recheck_seconds: float = 30.0,
    ) -> None:
        self._inner = inner
        self._llm_calls = llm_calls
        self._clock = clock
        self._limit_usd = limit_usd
        self._window_days = window_days
        self._recheck_seconds = recheck_seconds
        self._spent: float = 0.0
        self._checked_at: float = float("-inf")

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        caller: str = "core",
        purpose: str = "general",
        **kwargs: Any,
    ) -> tuple[str, UUID]:
        await self._enforce(caller=caller, purpose=purpose)
        return await self._inner.complete(
            messages, model=model, caller=caller, purpose=purpose, **kwargs
        )

    async def structured(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        model: str | None = None,
        caller: str = "core",
        purpose: str = "extraction",
        **kwargs: Any,
    ) -> tuple[dict[str, Any], UUID]:
        await self._enforce(caller=caller, purpose=purpose)
        return await self._inner.structured(
            messages, schema, model=model, caller=caller, purpose=purpose, **kwargs
        )

    async def spent(self) -> float:
        """Current spend in the rolling window, refreshing the cache if stale."""
        now = time.monotonic()
        if now - self._checked_at >= self._recheck_seconds:
            since = self._clock.now() - timedelta(days=self._window_days)
            self._spent = await self._llm_calls.total_cost_since(since)
            self._checked_at = now
        return self._spent

    async def _enforce(self, *, caller: str, purpose: str) -> None:
        spent = await self.spent()
        if spent >= self._limit_usd:
            logger.warning(
                "llm_budget_exceeded",
                spent=spent,
                limit=self._limit_usd,
                window_days=self._window_days,
                caller=caller,
                purpose=purpose,
            )
            raise BudgetExceeded(spent, self._limit_usd, self._window_days)

    def __getattr__(self, name: str) -> Any:
        """Pass through anything the port grows that this guard has not.

        Only reached for attributes not defined above, so ``complete`` and
        ``structured`` stay guarded.
        """
        return getattr(self._inner, name)
