"""Anthropic LLM adapter with call logging."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import anthropic

from research_engine.domain.errors import LLMError, LLMProviderDown, LLMRateLimited
from research_engine.domain.provenance import LLMCallDraft

if TYPE_CHECKING:
    from uuid import UUID

    from pydantic import SecretStr

    from research_engine.ports.repositories import LLMCallLogRepo


class AnthropicLLMAdapter:
    def __init__(
        self,
        api_key: SecretStr | None,
        call_log: LLMCallLogRepo,
        default_model: str = "claude-sonnet-4-5-20250929",
    ) -> None:
        key = api_key.get_secret_value() if api_key else None
        self._client = anthropic.AsyncAnthropic(api_key=key)
        self._call_log = call_log
        self._default_model = default_model

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        caller: str = "core",
        purpose: str = "general",
        **kwargs: Any,
    ) -> tuple[str, UUID]:
        model = model or self._default_model
        start = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=kwargs.get("max_tokens", 4096),
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            text = response.content[0].text
            call = await self._call_log.insert(
                LLMCallDraft(
                    purpose=purpose,
                    caller=caller,
                    model=model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cost_estimate=self._estimate_cost(
                        model, response.usage.input_tokens, response.usage.output_tokens
                    ),
                    duration_ms=duration_ms,
                    status="ok",
                )
            )
            return text, call.id
        except anthropic.RateLimitError as e:
            await self._log_error(model, caller, purpose, start, str(e))
            raise LLMRateLimited(str(e)) from e
        except anthropic.APIConnectionError as e:
            await self._log_error(model, caller, purpose, start, str(e))
            raise LLMProviderDown(str(e)) from e
        except anthropic.APIError as e:
            await self._log_error(model, caller, purpose, start, str(e))
            raise LLMError(str(e)) from e

    async def structured(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        model: str | None = None,
        caller: str = "core",
        purpose: str = "extraction",
        **kwargs: Any,
    ) -> tuple[dict[str, Any], UUID]:
        # Use tool_use for structured output
        model = model or self._default_model
        start = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=kwargs.get("max_tokens", 4096),
                tools=[
                    {
                        "name": "extract",
                        "description": "Extract structured data from the passage.",
                        "input_schema": schema,
                    }
                ],
                tool_choice={"type": "tool", "name": "extract"},
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            # Find the tool_use block
            tool_input = {}
            for block in response.content:
                if block.type == "tool_use":
                    tool_input = block.input
                    break

            call = await self._call_log.insert(
                LLMCallDraft(
                    purpose=purpose,
                    caller=caller,
                    model=model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cost_estimate=self._estimate_cost(
                        model, response.usage.input_tokens, response.usage.output_tokens
                    ),
                    duration_ms=duration_ms,
                    status="ok",
                )
            )
            return tool_input, call.id
        except anthropic.RateLimitError as e:
            await self._log_error(model, caller, purpose, start, str(e))
            raise LLMRateLimited(str(e)) from e
        except anthropic.APIConnectionError as e:
            await self._log_error(model, caller, purpose, start, str(e))
            raise LLMProviderDown(str(e)) from e
        except anthropic.APIError as e:
            await self._log_error(model, caller, purpose, start, str(e))
            raise LLMError(str(e)) from e

    async def _log_error(
        self, model: str, caller: str, purpose: str, start: float, error: str
    ) -> None:
        duration_ms = int((time.monotonic() - start) * 1000)
        await self._call_log.insert(
            LLMCallDraft(
                purpose=purpose,
                caller=caller,
                model=model,
                duration_ms=duration_ms,
                status="error",
                error=error,
            )
        )

    @staticmethod
    def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
        # Rough cost estimates per 1M tokens
        costs = {
            "claude-sonnet-4-5-20250929": (3.0, 15.0),
            "claude-haiku-4-5-20251001": (0.80, 4.0),
            "claude-opus-4-6": (15.0, 75.0),
        }
        in_rate, out_rate = costs.get(model, (3.0, 15.0))
        return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
