"""PDF text extraction module using pymupdf (fitz)."""

from __future__ import annotations

import asyncio
import mimetypes
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()


class PDFTextModule:
    id = "pdf_text"
    version = "1.0"
    supported_extensions = {".pdf"}
    supported_mime_types = {"application/pdf"}

    async def detect(self, source_path: Path) -> tuple[float, str]:
        """Detect whether the source is a PDF file."""
        suffix = source_path.suffix.lower()
        if suffix in self.supported_extensions:
            return 0.9, f"extension '{suffix}' matches PDF"

        mime, _ = mimetypes.guess_type(str(source_path))
        if mime in self.supported_mime_types:
            return 0.8, f"MIME type '{mime}' matches PDF"

        # Check the magic bytes
        try:
            loop = asyncio.get_event_loop()
            header = await loop.run_in_executor(None, self._read_bytes, source_path, 5)
            if header == b"%PDF-":
                return 0.9, "file starts with PDF magic bytes"
        except Exception:
            pass

        return 0.0, "not detected as PDF"

    async def parse(self, source_path: Path) -> tuple[str, str, dict]:
        """Extract text from a PDF page by page."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._extract, source_path)

    def default_chunker(self) -> str:
        return "prose_window"

    def default_document_type(self) -> str:
        return "generic"

    @staticmethod
    def _read_bytes(path: Path, n: int) -> bytes:
        with path.open("rb") as f:
            return f.read(n)

    @staticmethod
    def _extract(source_path: Path) -> tuple[str, str, dict]:
        import fitz  # pymupdf

        doc = fitz.open(str(source_path))
        try:
            pages_text: list[str] = []
            for page in doc:
                text = page.get_text("text")
                if text.strip():
                    pages_text.append(text)

            full_text = "\n\n".join(pages_text)

            # Try to get title from PDF metadata first
            pdf_meta = doc.metadata or {}
            title = pdf_meta.get("title") or ""
            if not title.strip():
                # Fall back to first line of text
                for line in full_text.splitlines():
                    stripped = line.strip()
                    if stripped and len(stripped) <= 300:
                        title = stripped
                        break
                else:
                    title = source_path.stem

            metadata = {
                "page_count": doc.page_count,
                "char_count": len(full_text),
                "file_name": source_path.name,
                "pdf_author": pdf_meta.get("author", ""),
                "pdf_subject": pdf_meta.get("subject", ""),
                "pdf_creator": pdf_meta.get("creator", ""),
                "pdf_creation_date": pdf_meta.get("creationDate", ""),
            }
            # Remove empty metadata values
            metadata = {k: v for k, v in metadata.items() if v != ""}

            return full_text, title, metadata
        finally:
            doc.close()
