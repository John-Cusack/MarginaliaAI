from __future__ import annotations

import pytest

from research_engine_sdk import PassageDraft, approx_tokens, cap_spans, split_at_boundary
from research_engine_sdk.testing import assert_chunker_contract


class BoundaryChunker:
    id = "boundary"
    version = "1"
    consumes = "text"
    max_passage_tokens = 100

    async def chunk(self, text: str, metadata=None):
        if not text.strip():
            return []
        spans = cap_spans(text, [(0, len(text))], self.max_passage_tokens)
        return [
            PassageDraft(
                position=position,
                char_start=start,
                char_end=end,
                text=text[start:end],
                token_count=approx_tokens(text[start:end]),
                chunker=self.id,
                chunker_version=self.version,
            )
            for position, (start, end) in enumerate(spans)
        ]


def test_split_preserves_absolute_spans() -> None:
    text = "prefix\nalpha beta gamma\nsuffix"
    spans = split_at_boundary(text, 7, 23, 8)

    assert "".join(text[start:end] for start, end in spans) == text[7:23]


@pytest.mark.asyncio
async def test_custom_chunker_meets_portable_contract() -> None:
    await assert_chunker_contract(
        BoundaryChunker(),
        texts={
            "ascii": "word " * 400,
            "greek": "λόγος " * 400,
            "cjk": "第一句。第二句。" * 400,
        },
    )
