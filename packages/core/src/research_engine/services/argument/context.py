"""Read-time context around a claim anchor or any other cited span."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel

from research_engine.domain.errors import NotFoundError
from research_engine.domain.nodes import DocumentNode  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Sequence



class AnchorContext(BaseModel):
    document_id: UUID
    char_start: int
    char_end: int
    window_start: int
    window_end: int
    window: str
    quote_offset_in_window: int
    quote_length: int
    containing_node: DocumentNode | None = None


class AnchorContextService:
    def __init__(
        self,
        spans: Any,
        document_texts: Any,
        document_nodes: Any,
    ) -> None:
        self._spans = spans
        self._texts = document_texts
        self._nodes = document_nodes

    async def for_span(self, span_id: UUID, window: int = 1200) -> AnchorContext:
        span = await self._spans.get(span_id)
        if span is None:
            raise NotFoundError("source_span", span_id)
        return await self.for_coordinates(
            span.document_id, span.char_start, span.char_end, window
        )

    async def for_coordinates(
        self, document_id: UUID, char_start: int, char_end: int, window: int = 1200
    ) -> AnchorContext:
        return (
            await self.many_for_coordinates(
                [(document_id, char_start, char_end)], window=window
            )
        )[0]

    async def many_for_coordinates(
        self,
        coordinates: Sequence[tuple[UUID, int, int]],
        *,
        window: int = 1200,
    ) -> list[AnchorContext]:
        if isinstance(window, bool) or window < 0:
            raise ValueError("window must be a non-negative integer")
        requests = []
        for document_id, char_start, char_end in coordinates:
            if char_start < 0 or char_end <= char_start:
                raise ValueError(
                    f"Span [{char_start}, {char_end}) is not a valid address."
                )
            requests.append((document_id, max(0, char_start - window), char_end + window))
        texts = await self._texts.get_spans(requests)
        nodes = await asyncio.gather(*(
            self._nodes.find_by_span(document_id, char_start, char_end)
            for document_id, char_start, char_end in coordinates
        ))
        contexts = []
        for coords, request, text, node in zip(
            coordinates, requests, texts, nodes, strict=True
        ):
            document_id, char_start, char_end = coords
            _, window_start, _ = request
            if text is None:
                raise NotFoundError("document_text", document_id)
            contexts.append(
                AnchorContext(
                    document_id=document_id,
                    char_start=char_start,
                    char_end=char_end,
                    window_start=window_start,
                    window_end=window_start + len(text),
                    window=text,
                    quote_offset_in_window=char_start - window_start,
                    quote_length=char_end - char_start,
                    containing_node=node,
                )
            )
        return contexts
