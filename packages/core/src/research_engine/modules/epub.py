"""EPUB ingestion module using ebooklib."""

from __future__ import annotations

import asyncio
import mimetypes
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()


class EPUBModule:
    id = "epub"
    version = "1.0"
    supported_extensions = {".epub"}
    supported_mime_types = {"application/epub+zip"}

    async def detect(self, source_path: Path) -> tuple[float, str]:
        """Detect whether the source is an EPUB file."""
        suffix = source_path.suffix.lower()
        if suffix in self.supported_extensions:
            return 0.9, f"extension '{suffix}' matches EPUB"

        mime, _ = mimetypes.guess_type(str(source_path))
        if mime in self.supported_mime_types:
            return 0.8, f"MIME type '{mime}' matches EPUB"

        # EPUB is a ZIP with specific first bytes
        try:
            loop = asyncio.get_event_loop()
            header = await loop.run_in_executor(None, self._read_bytes, source_path, 4)
            if header == b"PK\x03\x04":
                # Could be any ZIP; low confidence
                return 0.2, "file is a ZIP archive (could be EPUB)"
        except Exception:
            pass

        return 0.0, "not detected as EPUB"

    async def parse(self, source_path: Path) -> tuple[str, str, dict]:
        """Extract text from an EPUB file."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._extract, source_path)

    def default_chunker(self) -> str:
        return "structural"

    def default_document_type(self) -> str:
        return "book"

    @staticmethod
    def _read_bytes(path: Path, n: int) -> bytes:
        with path.open("rb") as f:
            return f.read(n)

    @staticmethod
    def _extract(source_path: Path) -> tuple[str, str, dict]:
        import ebooklib
        from bs4 import BeautifulSoup
        from ebooklib import epub

        book = epub.read_epub(str(source_path), options={"ignore_ncx": True})

        # Extract title
        title = book.get_metadata("DC", "title")
        title = title[0][0] if title else source_path.stem

        # Extract author
        authors = book.get_metadata("DC", "author")
        author = authors[0][0] if authors else ""

        # Extract language
        languages = book.get_metadata("DC", "language")
        language = languages[0][0] if languages else ""

        # Extract text from document items
        chapters: list[str] = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            html_content = item.get_content().decode("utf-8", errors="replace")
            soup = BeautifulSoup(html_content, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
            if text.strip():
                chapters.append(text)

        full_text = "\n\n".join(chapters)

        metadata: dict = {
            "chapter_count": len(chapters),
            "char_count": len(full_text),
            "file_name": source_path.name,
        }
        if author:
            metadata["author"] = author
        if language:
            metadata["language"] = language

        # Grab any other DC metadata
        for field in ("publisher", "date", "description", "identifier"):
            values = book.get_metadata("DC", field)
            if values and values[0][0]:
                metadata[f"dc_{field}"] = values[0][0]

        return full_text, title, metadata
