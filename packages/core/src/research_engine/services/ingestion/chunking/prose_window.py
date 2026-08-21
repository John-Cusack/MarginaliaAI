"""Prose window chunker — sentence-boundary-aware sliding window."""

from __future__ import annotations

import re

from research_engine.domain.passages import PassageDraft
from research_engine.services.ingestion.chunking.fixed_window import (
    cap_spans,
    split_at_boundary,
)
from research_engine.services.text.tokens import (
    approx_tokens,
    chars_per_token,
    token_budget_chars,
)

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
    # 4.0: token estimates are script-aware, so the window means the same thing
    # in Greek as in English. Boundaries move only in non-Latin text; an
    # ASCII-only document chunks byte-identically to 3.0.
    version = "4.0"

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

        # Measured once for the document: the script mix of a sentence is the
        # script mix of the book it came from, and measuring per span would put
        # an O(n) scan inside the loop below.
        rate = chars_per_token(text)

        # Sentence spans do not overlap, so capping them here is safe; the
        # overlap this chunker adds is expressed later, by widening a window's
        # start rather than by re-cutting.
        spans = cap_spans(
            text,
            [
                piece
                for span in sentence_spans(text)
                for piece in self._fit_to_window(text, span, rate)
            ],
            self._max_tokens,
        )
        if not spans:
            return []

        chunks: list[PassageDraft] = []
        window: list[tuple[int, int]] = []
        window_tokens = 0
        position = 0

        for span in spans:
            span_tokens = self._span_tokens(text, span, rate)

            if window and window_tokens + span_tokens > self._max_tokens:
                chunks.append(
                    self._make_draft(
                        text, window[0][0], window[-1][1], position, metadata, rate
                    )
                )
                position += 1
                window = self._overlap_window(text, window, rate)
                window_tokens = sum(self._span_tokens(text, s, rate) for s in window)

            window.append(span)
            window_tokens += span_tokens

        if window:
            chunks.append(
                self._make_draft(
                    text, window[0][0], window[-1][1], position, metadata, rate
                )
            )

        return chunks

    def _fit_to_window(
        self, text: str, span: tuple[int, int], rate: float
    ) -> list[tuple[int, int]]:
        """Break a unit that will not fit, at the best seam available."""
        return split_at_boundary(
            text, span[0], span[1], token_budget_chars(self._max_tokens, rate)
        )

    def _overlap_window(
        self, text: str, window: list[tuple[int, int]], rate: float
    ) -> list[tuple[int, int]]:
        """The trailing sentences of *window* that fit in the overlap budget."""
        overlap_tokens = 0
        keep_from = len(window)
        for i in range(len(window) - 1, -1, -1):
            span_tokens = self._span_tokens(text, window[i], rate)
            if overlap_tokens + span_tokens > self._overlap_tokens:
                break
            overlap_tokens += span_tokens
            keep_from = i
        return window[keep_from:]

    def _make_draft(
        self,
        text: str,
        start: int,
        end: int,
        position: int,
        metadata: dict | None,
        rate: float,
    ) -> PassageDraft:
        chunk_text = text[start:end]
        return PassageDraft(
            position=position,
            char_start=start,
            char_end=end,
            text=chunk_text,
            token_count=approx_tokens(chunk_text, rate),
            chunker=self.id,
            chunker_version=self.version,
            metadata=metadata or {},
        )

    @staticmethod
    def _span_tokens(text: str, span: tuple[int, int], rate: float) -> int:
        return approx_tokens(text[span[0] : span[1]], rate)
