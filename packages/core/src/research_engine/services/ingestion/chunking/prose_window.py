"""Prose window chunker — sentence-boundary-aware sliding window."""

from __future__ import annotations

import re

from research_engine.domain.passages import PassageDraft

# Simple sentence boundary pattern
_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentence spans as ``(start, end)`` offsets into *text*.

    Boundary whitespace falls between spans rather than inside them, so slicing
    from the first sentence's start to the last sentence's end recovers the
    original inter-sentence spacing exactly.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENT_BOUNDARY.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


class ProseWindowChunker:
    id = "prose_window"
    #: What `chunk()` takes: "text" or "sections".
    consumes = "text"
    # 3.0: a unit larger than the window is broken at a word or line boundary
    # instead of emitted whole. Found in the corpus, not in a fixture: a book
    # index has almost no sentence punctuation, so the whole of one became a
    # single 22,131-token passage — well past the 8,192 the embedder accepts,
    # which means most of it was stored but never embedded.
    version = "3.0"

    def __init__(self, max_tokens: int = 500, overlap_tokens: int = 50) -> None:
        self._max_tokens = max_tokens
        self._overlap_tokens = overlap_tokens

    @property
    def max_passage_tokens(self) -> int | None:
        return self._max_tokens

    async def chunk(self, text: str, metadata: dict | None = None) -> list[PassageDraft]:
        """Split text into overlapping chunks at sentence boundaries."""
        if not text.strip():
            return []

        spans = [
            piece
            for span in sentence_spans(text)
            for piece in self._fit_to_window(text, span)
        ]
        if not spans:
            return []

        chunks: list[PassageDraft] = []
        window: list[tuple[int, int]] = []
        window_tokens = 0
        position = 0

        for span in spans:
            span_tokens = self._span_tokens(text, span)

            if window and window_tokens + span_tokens > self._max_tokens:
                chunks.append(
                    self._make_draft(text, window[0][0], window[-1][1], position, metadata)
                )
                position += 1
                window = self._overlap_window(text, window)
                window_tokens = sum(self._span_tokens(text, s) for s in window)

            window.append(span)
            window_tokens += span_tokens

        if window:
            chunks.append(self._make_draft(text, window[0][0], window[-1][1], position, metadata))

        return chunks

    def _fit_to_window(self, text: str, span: tuple[int, int]) -> list[tuple[int, int]]:
        """Break a single unit that will not fit, preferring a real boundary.

        Sentence boundaries are the natural seam, but prose is not the only
        thing ingested: indexes, tables of contents and lexicon entries run for
        pages with barely a full stop. Treating those as one indivisible unit
        produced passages an order of magnitude over the window — and past the
        embedder's own limit, so the tail was silently not embedded at all.

        Line breaks are tried before spaces because in exactly those documents
        the line *is* the record.
        """
        start, end = span
        budget = self._max_tokens * 4  # the shared ~4-chars-per-token estimate
        if end - start <= budget:
            return [span]

        pieces: list[tuple[int, int]] = []
        cursor = start
        while end - cursor > budget:
            window_end = cursor + budget
            cut = text.rfind("\n", cursor + 1, window_end)
            if cut <= cursor:
                cut = text.rfind(" ", cursor + 1, window_end)
            if cut <= cursor:
                # No boundary of any kind: cut on the budget. A passage the
                # embedder truncates is worse than one cut mid-word.
                cut = window_end
            pieces.append((cursor, cut))
            cursor = cut
        if cursor < end:
            pieces.append((cursor, end))
        return [(s, e) for s, e in pieces if text[s:e].strip()]

    def _overlap_window(
        self, text: str, window: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """The trailing sentences of *window* that fit in the overlap budget."""
        overlap_tokens = 0
        keep_from = len(window)
        for i in range(len(window) - 1, -1, -1):
            span_tokens = self._span_tokens(text, window[i])
            if overlap_tokens + span_tokens > self._overlap_tokens:
                break
            overlap_tokens += span_tokens
            keep_from = i
        return window[keep_from:]

    def _make_draft(
        self, text: str, start: int, end: int, position: int, metadata: dict | None
    ) -> PassageDraft:
        chunk_text = text[start:end]
        return PassageDraft(
            position=position,
            char_start=start,
            char_end=end,
            text=chunk_text,
            token_count=self._approx_tokens(chunk_text),
            chunker=self.id,
            chunker_version=self.version,
            metadata=metadata or {},
        )

    def _span_tokens(self, text: str, span: tuple[int, int]) -> int:
        return self._approx_tokens(text[span[0] : span[1]])

    @staticmethod
    def _approx_tokens(text: str) -> int:
        """Rough token estimate: ~4 chars per token."""
        return max(1, len(text) // 4)
