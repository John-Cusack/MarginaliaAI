"""Metadata decorators for plugin tools and hooks."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def tool(
    id: str,
    description: str,
    input_schema: dict[str, Any] | None = None,
) -> Callable[[F], F]:
    """Attach MCP tool metadata without registering or importing core."""

    def decorator(fn: F) -> F:
        schema = input_schema or {"type": "object", "properties": {}}

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await fn(*args, **kwargs)

        wrapper._tool_id = id  # type: ignore[attr-defined]
        wrapper._tool_description = description  # type: ignore[attr-defined]
        wrapper._tool_input_schema = schema  # type: ignore[attr-defined]
        return cast("F", wrapper)

    return decorator


def hook(
    event: str = "post_ingestion",
    document_types: list[str] | None = None,
) -> Callable[[F], F]:
    """Attach lifecycle-hook metadata without registering the callable."""

    def decorator(fn: F) -> F:
        fn._hook_event = event  # type: ignore[attr-defined]
        fn._hook_document_types = list(document_types or [])  # type: ignore[attr-defined]
        return fn

    return decorator
