"""Fixed window chunker — simple character/token windows as fallback."""

from __future__ import annotations

from research_engine.domain.passages import PassageDraft
from research_engine.services.text.tokens import (
    DEFAULT_CHARS_PER_TOKEN,
    approx_tokens,
    chars_per_token,
    min_chars_per_token,
)

DEFAULT_WINDOW_CHARS = 2000
DEFAULT_OVERLAP_CHARS = 200


def trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Narrow ``(start, end)`` past surrounding whitespace.

    Trimming the *span* rather than the text is what keeps the offsets true:
    ``.strip()`` on the sliced text leaves the span describing a wider region
    than the text it is supposed to address.
    """
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def split_at_boundary(
    text: str, start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
    """Break ``[start, end)`` into pieces of at most *max_chars*, at real seams.

    Every chunker knows one kind of seam — sentences, paragraphs, verse
    references — and real documents contain long stretches with none of it: an
    index, a lexicon entry, a table. Emitting such a stretch whole is how four
    separate chunkers came to write passages an order of magnitude over their
    own limit, and past what an embedding model will accept, so the tail was
    stored but never embedded.

    Line breaks are preferred to spaces because in exactly those documents the
    line is the record. A stretch with no seam at all is cut on the budget: a
    passage the embedder truncates is worse than one cut mid-word.
    """
    if end - start <= max_chars:
        return [(start, end)]

    pieces: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > max_chars:
        window_end = cursor + max_chars
        cut = text.rfind("\n", cursor + 1, window_end)
        if cut <= cursor:
            cut = text.rfind(" ", cursor + 1, window_end)
        if cut <= cursor:
            cut = window_end
        pieces.append((cursor, cut))
        cursor = cut
    if cursor < end:
        pieces.append((cursor, end))
    return [(s, e) for s, e in pieces if text[s:e].strip()]


def cap_spans(
    text: str, spans: list[tuple[int, int]], max_tokens: int
) -> list[tuple[int, int]]:
    """Re-split any span that measures over *max_tokens* on its own content.

    A document-level chars-per-token rate is the right basis for a window
    budget — it is what the document is mostly made of — but it is an average,
    and averages do not bound anything. BDAG measured 3.32 characters per token
    as a whole while its Greek runs measure 1.50, so a span budgeted on the
    average and filled with the dense part came out at 960 tokens against a
    declared cap of 500.

    Budgeting everything on the densest script instead does hold the cap, but it
    holds it everywhere: on the same articles it turned 1,092 passages into
    2,506, halving the size of the English ones too, which are not the problem.

    So the average sets the budget, and this pass catches the spans where the
    average lied — re-splitting only those, against their own density. Passages
    stay as large as they safely can be, and the cap becomes a fact rather than
    an aspiration.
    """
    capped: list[tuple[int, int]] = []
    for start, end in spans:
        piece = text[start:end]
        if approx_tokens(piece) <= max_tokens:
            capped.append((start, end))
            continue
        budget = max(1, int(max_tokens * min_chars_per_token(piece)))
        capped.extend(split_at_boundary(text, start, end, budget))
    return capped


class FixedWindowChunker:
    id = "fixed_window"
    #: What `chunk()` takes: "text" or "sections".
    consumes = "text"
    # 2.0: trims the span instead of stripping the text, so char offsets and
    # text agree. Offsets written by 1.0 are off by the stripped whitespace.
    # 3.0: the window is a *token* budget expressed in characters, so it holds
    # the same amount of text in every script. An ASCII document is unchanged;
    # a CJK one now gets windows of about 750 characters rather than 2,000,
    # which is the same 500 tokens.
    version = "3.0"

    def __init__(
        self, window_chars: int = DEFAULT_WINDOW_CHARS, overlap_chars: int = DEFAULT_OVERLAP_CHARS
    ) -> None:
        self._window = window_chars
        self._overlap = overlap_chars

    @property
    def max_passage_tokens(self) -> int | None:
        """The window as a token budget, now honoured in every script."""
        return max(1, int(self._window / DEFAULT_CHARS_PER_TOKEN))

    async def chunk(self, text: str, metadata: dict | None = None) -> list[PassageDraft]:
        if not text.strip():
            return []

        # The configured window is characters of *English*. Re-derive it for
        # whatever script this actually is, so the token budget is the constant.
        rate = chars_per_token(text)
        scale = rate / DEFAULT_CHARS_PER_TOKEN
        window = max(1, int(self._window * scale))
        overlap = max(0, int(self._overlap * scale))

        chunks = []
        start = 0
        position = 0
        while start < len(text):
            end = min(start + window, len(text))
            span_start, span_end = trim_span(text, start, end)
            if span_end > span_start:
                chunk_text = text[span_start:span_end]
                chunks.append(
                    PassageDraft(
                        position=position,
                        char_start=span_start,
                        char_end=span_end,
                        text=chunk_text,
                        token_count=approx_tokens(chunk_text, rate),
                        chunker=self.id,
                        chunker_version=self.version,
                        metadata=metadata or {},
                    )
                )
                position += 1
            if end >= len(text):
                break
            # max(..., start + 1) so an overlap >= window cannot stall the walk.
            start = max(end - overlap, start + 1)

        return chunks
