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


class FixedWindowChunker:
    id = "fixed_window"
    # 2.0: trims the span instead of stripping the text, so char offsets and
    # text agree. Offsets written by 1.0 are off by the stripped whitespace.
    version = "2.0"

    def __init__(
        self, window_chars: int = DEFAULT_WINDOW_CHARS, overlap_chars: int = DEFAULT_OVERLAP_CHARS
    ) -> None:
        self._window = window_chars
        self._overlap = overlap_chars

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
