"""Whole-or-paragraph chunker — short docs stay whole, long docs split by paragraph."""

from __future__ import annotations

import re

from research_engine.domain.passages import PassageDraft
from research_engine.services.ingestion.chunking.fixed_window import trim_span

_PARA_SPLIT = re.compile(r"\n\s*\n")
DEFAULT_THRESHOLD_TOKENS = 600


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Paragraph spans as ``(start, end)`` offsets, blank ones dropped."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _PARA_SPLIT.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))

    trimmed = [trim_span(text, s, e) for s, e in spans]
    return [(s, e) for s, e in trimmed if e > s]


class WholeOrParagraphChunker:
    id = "whole_or_paragraph"
    #: What `chunk()` takes: "text" or "sections".
    consumes = "text"
    # 2.0: emits char offsets, drops empty input, and numbers positions densely.
    version = "2.0"

    def __init__(self, threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS) -> None:
        self._threshold = threshold_tokens

    @property
    def max_passage_tokens(self) -> int | None:
        """Unbounded, deliberately.

        A document that is one long paragraph yields one long passage: this
        chunker's promise is to respect paragraph boundaries, and there is no
        smaller unit it is willing to break. Declared None rather than left
        unstated so the contract suite treats it as a decision, not an
        oversight — which is exactly how `structural` slipped past unbounded.
        """
        return None

    async def chunk(self, text: str, metadata: dict | None = None) -> list[PassageDraft]:
        if not text.strip():
            return []

        approx_tokens = max(1, len(text) // 4)
        if approx_tokens <= self._threshold:
            return [self._draft(text, 0, len(text), 0, metadata)]

        return [
            self._draft(text, start, end, position, metadata)
            for position, (start, end) in enumerate(paragraph_spans(text))
        ]

    def _draft(
        self, text: str, start: int, end: int, position: int, metadata: dict | None
    ) -> PassageDraft:
        chunk_text = text[start:end]
        return PassageDraft(
            position=position,
            char_start=start,
            char_end=end,
            text=chunk_text,
            token_count=max(1, len(chunk_text) // 4),
            chunker=self.id,
            chunker_version=self.version,
            metadata=metadata or {},
        )
