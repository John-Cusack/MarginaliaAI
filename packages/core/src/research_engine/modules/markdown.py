"""Markdown ingestion module."""

from __future__ import annotations

import asyncio
import mimetypes
import re
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()

# Patterns for stripping markdown formatting
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BOLD_ITALIC = re.compile(r"(\*{1,3}|_{1,3})(.+?)\1")
_STRIKETHROUGH = re.compile(r"~~(.+?)~~")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_BLOCKQUOTE = re.compile(r"^>\s?", re.MULTILINE)
_HORIZONTAL_RULE = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_LIST_MARKER = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_ORDERED_LIST = re.compile(r"^(\s*)\d+\.\s+", re.MULTILINE)


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting, keeping the readable text."""
    result = text
    result = _CODE_BLOCK.sub("", result)
    result = _IMAGE.sub(r"\1", result)
    result = _LINK.sub(r"\1", result)
    result = _BOLD_ITALIC.sub(r"\2", result)
    result = _STRIKETHROUGH.sub(r"\1", result)
    result = _INLINE_CODE.sub(r"\1", result)
    result = _HEADING.sub("", result)
    result = _HTML_TAG.sub("", result)
    result = _BLOCKQUOTE.sub("", result)
    result = _HORIZONTAL_RULE.sub("", result)
    result = _LIST_MARKER.sub(r"\1", result)
    result = _ORDERED_LIST.sub(r"\1", result)
    # Collapse multiple blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _extract_title(text: str, fallback: str) -> str:
    """Extract the first heading as the title."""
    match = re.search(r"^#{1,6}\s+(.+)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return fallback


def _count_headings(text: str) -> int:
    return len(re.findall(r"^#{1,6}\s+", text, re.MULTILINE))


class MarkdownModule:
    id = "markdown"
    version = "1.0"
    supported_extensions = {".md", ".markdown", ".mdown", ".mkd"}
    supported_mime_types = {"text/markdown", "text/x-markdown"}

    async def detect(self, source_path: Path) -> tuple[float, str]:
        """Detect whether the source is a markdown file."""
        suffix = source_path.suffix.lower()
        if suffix in self.supported_extensions:
            return 0.9, f"extension '{suffix}' matches markdown"

        mime, _ = mimetypes.guess_type(str(source_path))
        if mime in self.supported_mime_types:
            return 0.8, f"MIME type '{mime}' matches markdown"

        # Peek at content for markdown indicators
        try:
            loop = asyncio.get_event_loop()
            head = await loop.run_in_executor(None, self._read_head, source_path)
            if re.search(r"^#{1,6}\s+", head, re.MULTILINE):
                return 0.4, "file contains markdown headings"
        except Exception:
            pass

        return 0.0, "not detected as markdown"

    async def parse(self, source_path: Path) -> tuple[str, str, dict]:
        """Parse a markdown file, stripping formatting."""
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, self._read_file, source_path)

        title = _extract_title(raw, source_path.stem)
        full_text = _strip_markdown(raw)

        metadata = {
            "char_count": len(full_text),
            "heading_count": _count_headings(raw),
            "file_name": source_path.name,
            "format": "markdown",
        }

        return full_text, title, metadata

    def default_chunker(self) -> str:
        return "structural"

    def default_document_type(self) -> str:
        return "generic"

    @staticmethod
    def _read_file(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _read_head(path: Path, size: int = 4096) -> str:
        with path.open("r", encoding="utf-8") as f:
            return f.read(size)
