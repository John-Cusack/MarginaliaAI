"""LLM port interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from uuid import UUID


@runtime_checkable
class LLMPort(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        caller: str = "core",
        purpose: str = "general",
        **kwargs: Any,
    ) -> tuple[str, UUID]:
        """Complete a prompt. Returns (response_text, llm_call_id)."""
        ...

    async def structured(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        model: str | None = None,
        caller: str = "core",
        purpose: str = "extraction",
        **kwargs: Any,
    ) -> tuple[dict[str, Any], UUID]:
        """Complete with structured output. Returns (parsed_json, llm_call_id)."""
        ...
