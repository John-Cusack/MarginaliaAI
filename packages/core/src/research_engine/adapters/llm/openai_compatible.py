"""OpenAI-compatible LLM adapter."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import openai

from research_engine.domain.errors import LLMError, LLMProviderDown, LLMRateLimited
from research_engine.domain.provenance import LLMCallDraft

if TYPE_CHECKING:
    from uuid import UUID

    from research_engine.ports.repositories import LLMCallLogRepo


class OpenAICompatibleLLMAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        call_log: LLMCallLogRepo,
        default_model: str = "gpt-4o",
    ) -> None:
        self._client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key or "unused")
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
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=kwargs.get("max_tokens", 4096),
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            text = response.choices[0].message.content or ""
            usage = response.usage
            call = await self._call_log.insert(
                LLMCallDraft(
                    purpose=purpose,
                    caller=caller,
                    model=model,
                    input_tokens=usage.prompt_tokens if usage else None,
                    output_tokens=usage.completion_tokens if usage else None,
                    duration_ms=duration_ms,
                    status="ok",
                )
            )
            return text, call.id
        except openai.RateLimitError as e:
            raise LLMRateLimited(str(e)) from e
        except openai.APIConnectionError as e:
            raise LLMProviderDown(str(e)) from e
        except openai.APIError as e:
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
        model = model or self._default_model
        start = time.monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=kwargs.get("max_tokens", 4096),
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "extract",
                            "description": "Extract structured data",
                            "parameters": schema,
                        },
                    }
                ],
                tool_choice={"type": "function", "function": {"name": "extract"}},
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            tool_call = response.choices[0].message.tool_calls[0]  # type: ignore[index]
            parsed = json.loads(tool_call.function.arguments)
            usage = response.usage
            call = await self._call_log.insert(
                LLMCallDraft(
                    purpose=purpose,
                    caller=caller,
                    model=model,
                    input_tokens=usage.prompt_tokens if usage else None,
                    output_tokens=usage.completion_tokens if usage else None,
                    duration_ms=duration_ms,
                    status="ok",
                )
            )
            return parsed, call.id
        except openai.RateLimitError as e:
            raise LLMRateLimited(str(e)) from e
        except openai.APIConnectionError as e:
            raise LLMProviderDown(str(e)) from e
        except openai.APIError as e:
            raise LLMError(str(e)) from e
