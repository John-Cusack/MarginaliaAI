"""Clock adapter implementations."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Real system clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Fake clock for testing."""

    def __init__(self, fixed: datetime | None = None) -> None:
        self._now = fixed or datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt
