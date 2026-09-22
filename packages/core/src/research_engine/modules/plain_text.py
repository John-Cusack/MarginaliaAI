"""Plain text ingestion module."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from typing import TYPE_CHECKING

import structlog

from research_engine import _rust as _rust_backend

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()


class PlainTextModule:
    id = "plain_text"
    version = "1.0"
    supported_extensions = {".txt"}
    supported_mime_types = {"text/plain"}

    async def detect(self, source_path: Path) -> tuple[float, str]:
        """Detect whether the source is a plain text file."""
        suffix = source_path.suffix.lower()
        if suffix in self.supported_extensions:
            return 0.8, f"extension '{suffix}' matches plain text"

        mime, _ = mimetypes.guess_type(str(source_path))
        if mime in self.supported_mime_types:
            return 0.7, f"MIME type '{mime}' matches plain text"

        rs = _rust_backend.rust_parse()
        if rs is not None:
            loop = asyncio.get_event_loop()
            head = await loop.run_in_executor(None, self._read_head_bytes, source_path)
            # Universal newlines, like the text-mode read below: a lone CR
            # must not change what the peek sees.
            head = head.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            return rs.detect_plain_text_content(head)

        # Check if readable as UTF-8 as a fallback
        try:
            loop = asyncio.get_event_loop()
            head = await loop.run_in_executor(None, self._read_head, source_path)
            head.encode("utf-8")
            return 0.3, "file is valid UTF-8 text (fallback detection)"
        except Exception:
            return 0.0, "not detected as plain text"

    async def parse(self, source_path: Path) -> tuple[str, str, dict]:
        """Parse a plain text file, returning (full_text, title, metadata)."""
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, self._read_file, source_path)

        rs = _rust_backend.rust_parse()
        if rs is not None:
            return _parse_plain_text_rs(rs, text, source_path)

        title = source_path.stem
        lines = text.splitlines()
        # Use the first non-empty line as title if short enough
        for line in lines:
            stripped = line.strip()
            if stripped and len(stripped) <= 200:
                title = stripped
                break

        metadata = {
            "char_count": len(text),
            "line_count": len(lines),
            "file_name": source_path.name,
        }

        return text, title, metadata

    def default_chunker(self) -> str:
        return "prose_window"

    def default_document_type(self) -> str:
        return "generic"

    @staticmethod
    def _read_file(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _read_head(path: Path, size: int = 8192) -> str:
        with path.open("r", encoding="utf-8") as f:
            return f.read(size)

    @staticmethod
    def _read_head_bytes(path: Path, size: int = 8192) -> bytes:
        with path.open("rb") as f:
            return f.read(size)


def _parse_plain_text_rs(rs, text, source_path):
    """`PlainTextModule.parse` via the Rust backend (see `research_engine._rust`).

    The file read stays caller-side (strict UTF-8 and universal newlines
    surface exactly as today); titles, counts, and metadata come back as
    `ParsedDocument` JSON with strings and integers only.
    """
    doc = json.loads(rs.parse_plain_text(text, source_path.name))
    return doc["text"], doc["title"], doc["metadata"]
