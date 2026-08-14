"""Async SQLAlchemy engine setup and transaction management."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from research_engine.ports.repositories import Transaction

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def build_engine(db_url: str, **kwargs: object) -> AsyncEngine:
    """Create an async SQLAlchemy engine."""
    engine = create_async_engine(
        db_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        **kwargs,
    )
    return engine


@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[Transaction]:
    """Open a transactional connection."""
    async with engine.begin() as conn:
        yield Transaction(conn)
