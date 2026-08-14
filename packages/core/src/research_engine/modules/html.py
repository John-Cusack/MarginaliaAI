"""HTML ingestion module using BeautifulSoup."""

from __future__ import annotations

import asyncio
import mimetypes
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()


class HTMLModule:
    id = "html"
    version = "1.0"
    supported_extensions = {".html", ".htm"}
    supported_mime_types = {"text/html", "application/xhtml+xml"}

    async def detect(self, source_path: Path) -> tuple[float, str]:
        """Detect whether the source is an HTML file."""
        suffix = source_path.suffix.lower()
        if suffix in self.supported_extensions:
            return 0.9, f"extension '{suffix}' matches HTML"

        mime, _ = mimetypes.guess_type(str(source_path))
        if mime in self.supported_mime_types:
            return 0.8, f"MIME type '{mime}' matches HTML"

        # Peek at content for HTML indicators
        try:
            loop = asyncio.get_event_loop()
            head = await loop.run_in_executor(None, self._read_head, source_path)
            lower = head.lower()
            if "<html" in lower or "<!doctype html" in lower:
                return 0.7, "file contains HTML markers"
        except Exception:
            pass

        return 0.0, "not detected as HTML"

    async def parse(self, source_path: Path) -> tuple[str, str, dict]:
        """Extract text from an HTML file."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._extract, source_path)

    def default_chunker(self) -> str:
        return "structural"

    def default_document_type(self) -> str:
        return "generic"

    @staticmethod
    def _read_head(path: Path, size: int = 4096) -> str:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(size)

    @staticmethod
    def _extract(source_path: Path) -> tuple[str, str, dict]:
        from bs4 import BeautifulSoup

        raw = source_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(raw, "html.parser")

        # Remove script and style elements
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        # Extract title
        title_tag = soup.find("title")
        h1_tag = soup.find("h1")
        if title_tag and title_tag.string:
            title = title_tag.string.strip()
        elif h1_tag:
            title = h1_tag.get_text(strip=True)
        else:
            title = source_path.stem

        # Extract body text
        body = soup.find("body")
        target = body if body else soup
        full_text = target.get_text(separator="\n", strip=True)

        # Extract meta tags
        meta_description = ""
        meta_author = ""
        meta_language = ""

        for meta in soup.find_all("meta"):
            name = (meta.get("name") or meta.get("property") or "").lower()
            content = meta.get("content", "")
            if name == "description":
                meta_description = content
            elif name == "author":
                meta_author = content
            elif name == "language":
                meta_language = content

        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            meta_language = meta_language or html_tag["lang"]

        # Count structural elements
        heading_count = len(soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]))
        link_count = len(soup.find_all("a", href=True))

        metadata: dict = {
            "char_count": len(full_text),
            "heading_count": heading_count,
            "link_count": link_count,
            "file_name": source_path.name,
        }
        if meta_description:
            metadata["description"] = meta_description
        if meta_author:
            metadata["author"] = meta_author
        if meta_language:
            metadata["language"] = meta_language

        return full_text, title, metadata
