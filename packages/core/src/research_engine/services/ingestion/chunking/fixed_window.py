"""Fixed window chunker — simple character/token windows as fallback."""

from __future__ import annotations

from research_engine.domain.passages import PassageDraft

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


class FixedWindowChunker:
    id = "fixed_window"
    #: What `chunk()` takes: "text" or "sections".
    consumes = "text"
    # 2.0: trims the span instead of stripping the text, so char offsets and
    # text agree. Offsets written by 1.0 are off by the stripped whitespace.
    version = "2.0"

    def __init__(
        self, window_chars: int = DEFAULT_WINDOW_CHARS, overlap_chars: int = DEFAULT_OVERLAP_CHARS
    ) -> None:
        self._window = window_chars
        self._overlap = overlap_chars

    @property
    def max_passage_tokens(self) -> int | None:
        """The window, in tokens, at the shared ~4-chars-per-token estimate."""
        return max(1, self._window // 4)

    async def chunk(self, text: str, metadata: dict | None = None) -> list[PassageDraft]:
        if not text.strip():
            return []

        chunks = []
        start = 0
        position = 0
        while start < len(text):
            end = min(start + self._window, len(text))
            span_start, span_end = trim_span(text, start, end)
            if span_end > span_start:
                chunk_text = text[span_start:span_end]
                chunks.append(
                    PassageDraft(
                        position=position,
                        char_start=span_start,
                        char_end=span_end,
                        text=chunk_text,
                        token_count=max(1, len(chunk_text) // 4),
                        chunker=self.id,
                        chunker_version=self.version,
                        metadata=metadata or {},
                    )
                )
                position += 1
            if end >= len(text):
                break
            # max(..., start + 1) so an overlap >= window cannot stall the walk.
            start = max(end - self._overlap, start + 1)

        return chunks
