"""httpx-based HTTP adapter."""

from __future__ import annotations

from typing import Any

import httpx


class HttpxAdapter:
    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def get(self, url: str, **kwargs: Any) -> bytes:
        resp = await self._client.get(url, **kwargs)
        resp.raise_for_status()
        return resp.content

    async def post(self, url: str, json: Any = None, **kwargs: Any) -> bytes:
        resp = await self._client.post(url, json=json, **kwargs)
        resp.raise_for_status()
        return resp.content

    async def close(self) -> None:
        await self._client.aclose()
