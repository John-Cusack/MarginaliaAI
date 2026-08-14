"""Tests for chunking strategies."""

from __future__ import annotations

import pytest

from research_engine.services.ingestion.chunking.fixed_window import FixedWindowChunker
from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker
from research_engine.services.ingestion.chunking.whole_or_paragraph import WholeOrParagraphChunker


class TestProseWindowChunker:
    @pytest.mark.asyncio
    async def test_short_text_single_chunk(self):
        chunker = ProseWindowChunker(max_tokens=500)
        chunks = await chunker.chunk("This is a short sentence.")
        assert len(chunks) == 1
        assert chunks[0].text == "This is a short sentence."
        assert chunks[0].position == 0

    @pytest.mark.asyncio
    async def test_long_text_multiple_chunks(self):
        # Create text that's > 500 tokens (~2000 chars)
        text = ". ".join([f"Sentence number {i} with some extra words to fill space" for i in range(100)])
        chunker = ProseWindowChunker(max_tokens=100, overlap_tokens=20)
        chunks = await chunker.chunk(text)
        assert len(chunks) > 1
        # Positions should be sequential
        for i, chunk in enumerate(chunks):
            assert chunk.position == i

    @pytest.mark.asyncio
    async def test_empty_text(self):
        chunker = ProseWindowChunker()
        chunks = await chunker.chunk("")
        assert len(chunks) == 0

    @pytest.mark.asyncio
    async def test_metadata_propagated(self):
        chunker = ProseWindowChunker()
        chunks = await chunker.chunk("Test text.", {"source": "test"})
        assert chunks[0].metadata == {"source": "test"}


class TestWholeOrParagraphChunker:
    @pytest.mark.asyncio
    async def test_short_text_stays_whole(self):
        chunker = WholeOrParagraphChunker(threshold_tokens=1000)
        chunks = await chunker.chunk("Short text here.")
        assert len(chunks) == 1

    @pytest.mark.asyncio
    async def test_long_text_splits_by_paragraph(self):
        text = "Paragraph one with some content.\n\nParagraph two with different content.\n\nParagraph three."
        chunker = WholeOrParagraphChunker(threshold_tokens=5)  # Very low threshold
        chunks = await chunker.chunk(text)
        assert len(chunks) == 3

    @pytest.mark.asyncio
    async def test_empty_paragraphs_skipped(self):
        text = "Para one.\n\n\n\nPara two."
        chunker = WholeOrParagraphChunker(threshold_tokens=2)
        chunks = await chunker.chunk(text)
        assert len(chunks) == 2


class TestFixedWindowChunker:
    @pytest.mark.asyncio
    async def test_basic(self):
        text = "A" * 5000
        chunker = FixedWindowChunker(window_chars=2000, overlap_chars=200)
        chunks = await chunker.chunk(text)
        assert len(chunks) >= 2

    @pytest.mark.asyncio
    async def test_short_text_single_chunk(self):
        chunker = FixedWindowChunker(window_chars=2000)
        chunks = await chunker.chunk("Short text.")
        assert len(chunks) == 1

    @pytest.mark.asyncio
    async def test_empty(self):
        chunker = FixedWindowChunker()
        chunks = await chunker.chunk("")
        assert len(chunks) == 0
