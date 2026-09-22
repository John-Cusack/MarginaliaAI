"""Fixed window chunker — simple character/token windows as fallback."""

from __future__ import annotations

from research_engine import _rust as _rust_backend
from research_engine_sdk import PassageDraft
from research_engine_sdk.chunking import (
    DEFAULT_CHARS_PER_TOKEN,
    approx_tokens,
    chars_per_token,
    trim_span,
)

DEFAULT_WINDOW_CHARS = 2000
DEFAULT_OVERLAP_CHARS = 200




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
        rs = _rust_backend.rust_chunk()
        if rs is not None:
            return _chunk_fixed_rs(rs, self, text, metadata)

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


def _chunk_fixed_rs(rs, chunker, text, metadata):
    """`FixedWindowChunker.chunk` via the Rust backend (see `research_engine._rust`).

    Drafts cross as JSON without metadata (the crate never reads it); the
    original mapping is reattached here, so identity holds on either backend.
    """
    return [
        PassageDraft.model_validate_json(raw).model_copy(update={"metadata": metadata or {}})
        for raw in rs.chunk_fixed(text, chunker._window, chunker._overlap)
    ]
