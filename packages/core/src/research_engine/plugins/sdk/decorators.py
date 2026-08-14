"""Decorators for plugin tools and hooks."""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def tool(
    id: str,
    description: str,
    input_schema: dict[str, Any] | None = None,
) -> Callable:
    """Decorator to register a function as an MCP tool."""

    def decorator(fn: Callable) -> Callable:
        fn._tool_id = id  # type: ignore[attr-defined]
        fn._tool_description = description  # type: ignore[attr-defined]
        fn._tool_input_schema = input_schema or {}  # type: ignore[attr-defined]

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await fn(*args, **kwargs)

        wrapper._tool_id = id  # type: ignore[attr-defined]
        wrapper._tool_description = description  # type: ignore[attr-defined]
        wrapper._tool_input_schema = input_schema or {}  # type: ignore[attr-defined]
        return wrapper

    return decorator


def hook(event: str = "post_ingestion", document_types: list[str] | None = None) -> Callable:
    """Decorator to register a function as a lifecycle hook."""

    def decorator(fn: Callable) -> Callable:
        fn._hook_event = event  # type: ignore[attr-defined]
        fn._hook_document_types = document_types or []  # type: ignore[attr-defined]
        return fn

    return decorator
