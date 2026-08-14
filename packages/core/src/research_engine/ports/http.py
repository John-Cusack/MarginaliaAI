"""HTTP port interface."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class HttpPort(Protocol):
    async def get(self, url: str, **kwargs: Any) -> bytes:
        """Fetch bytes from a URL."""
        ...

    async def post(self, url: str, json: Any = None, **kwargs: Any) -> bytes:
        """POST to a URL."""
        ...

    async def close(self) -> None: ...
