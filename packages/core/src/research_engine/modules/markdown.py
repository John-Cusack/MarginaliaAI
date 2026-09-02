"""Markdown ingestion module."""

from __future__ import annotations

import asyncio
import mimetypes
import re
from typing import TYPE_CHECKING

import structlog

from research_engine.services.text.sections import sections_from_markdown

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()

# Patterns for stripping markdown formatting. Heading markers are
# deliberately absent — see `_strip_markdown`.
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
    """Remove markdown formatting, keeping the readable text and the headings.

    Heading markers stay. They are not decoration to be cleaned away: they are
    the document's structure, and `sections_from_markdown` reads them back out
    of the canonical text to build the node tree and to bound a search window.
    Stripping them here destroyed that structure at the only moment it existed
    — the canonical text is what every later pass reads, so a heading deleted
    on the way in cannot be recovered by any reindex, only by parsing the
    source file again.

    Docling already leaves `#` lines in the markdown it exports, so a markdown
    file and a converted PDF now reach storage in the same shape.
    """
    result = text
    result = _CODE_BLOCK.sub("", result)
    result = _IMAGE.sub(r"\1", result)
    result = _LINK.sub(r"\1", result)
    result = _BOLD_ITALIC.sub(r"\2", result)
    result = _STRIKETHROUGH.sub(r"\1", result)
    result = _INLINE_CODE.sub(r"\1", result)
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


class MarkdownModule:
    id = "markdown"
    # 2.0: heading markers survive into the canonical text and a section table
    # is emitted. Documents parsed at 1.0 have their headings stripped out for
    # good, so they need re-ingesting — a reindex reads the stored text and
    # would find nothing there either.
    version = "2.0"
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
        sections = sections_from_markdown(full_text)

        metadata = {
            "char_count": len(full_text),
            "heading_count": len(sections),
            "file_name": source_path.name,
            "format": "markdown",
            # Boundaries only, addressed into the canonical text — the same
            # contract EPUB's table keeps. Without it the structural chunker
            # this module asks for silently fell back to prose windows, because
            # the pipeline reads a missing table as "this format has no
            # structure" rather than "this parser forgot to say".
            "sections": sections,
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
