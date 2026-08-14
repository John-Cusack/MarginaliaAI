"""Clock port interface for testability."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime


@runtime_checkable
class ClockPort(Protocol):
    def now(self) -> datetime:
        """Return the current UTC datetime."""
        ...
