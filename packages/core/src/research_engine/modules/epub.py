"""EPUB ingestion module using ebooklib."""

from __future__ import annotations

import asyncio
import mimetypes
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

    from bs4 import BeautifulSoup

logger = structlog.get_logger()


class EPUBModule:
    id = "epub"
    # 2.0: follows the spine for reading order, excludes the navigation
    # document, and emits structural sections with offsets into the canonical
    # text. Documents parsed by 1.0 have a different canonical text and must be
    # re-ingested, not re-chunked.
    version = "2.0"
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
        from bs4 import BeautifulSoup
        from ebooklib import epub

        # ebooklib defaults to ignore_ncx=True, which discards the EPUB2 table
        # of contents. We want it: the NCX carries the book's own chapter
        # titles, and EPUB3's nav document is not always present.
        book = epub.read_epub(str(source_path), options={"ignore_ncx": False})

        # Extract title
        title = book.get_metadata("DC", "title")
        title = title[0][0] if title else source_path.stem

        # Extract author
        authors = book.get_metadata("DC", "author")
        author = authors[0][0] if authors else ""

        # Extract language
        languages = book.get_metadata("DC", "language")
        language = languages[0][0] if languages else ""

        toc = _flatten_toc(book.toc)

        # Walk the spine, not the manifest. The manifest is a bag of files in
        # packaging order; only the spine states reading order, and the two
        # routinely disagree. Concatenating in manifest order yields a canonical
        # text whose chapters are shuffled, which silently corrupts every
        # passage offset addressed against it.
        separator = "\n\n"
        chapters: list[str] = []
        sections: list[dict] = []
        cursor = 0

        for idref, _linear in book.spine:
            item = book.get_item_with_id(idref)
            if item is None or isinstance(item, epub.EpubNav):
                # The navigation document is apparatus, not prose. Ingesting it
                # adds a phantom chapter whose text is the table of contents.
                continue

            html_content = item.get_content().decode("utf-8", errors="replace")
            soup = BeautifulSoup(html_content, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
            if not text.strip():
                continue

            href = item.get_name()
            heading, level = toc.get(href, (None, None))
            if heading is None:
                heading, level = _heading_from_markup(soup)

            section: dict = {"char_start": cursor, "char_end": cursor + len(text)}
            if heading:
                section["heading"] = heading
            if level:
                section["level"] = level
            if href:
                section["href"] = href
            sections.append(section)

            chapters.append(text)
            cursor += len(text) + len(separator)

        full_text = separator.join(chapters)

        metadata: dict = {
            "chapter_count": len(chapters),
            "char_count": len(full_text),
            "file_name": source_path.name,
            # Boundaries only — never the section text. These address into the
            # canonical text the same way passages do, so they cost nothing to
            # store and survive re-chunking.
            "sections": sections,
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


def _flatten_toc(entries: object, depth: int = 1) -> dict[str, tuple[str, int]]:
    """Map each table-of-contents target to its ``(title, depth)``.

    ``book.toc`` mixes bare ``Link`` entries with ``(Section, children)`` tuples
    and nests to arbitrary depth. Fragments are discarded — the spine addresses
    whole files, so ``c1.xhtml#part2`` and ``c1.xhtml`` name the same target
    here. The shallowest entry for a target wins, which keeps a chapter titled
    by its chapter heading rather than by its first subsection.
    """
    flat: dict[str, tuple[str, int]] = {}
    for entry in entries or ():
        if isinstance(entry, (list, tuple)):
            node = entry[0]
            children = entry[1] if len(entry) > 1 else ()
        else:
            node, children = entry, ()

        href = getattr(node, "href", None)
        title = getattr(node, "title", None)
        if href and title:
            flat.setdefault(href.split("#", 1)[0], (title, depth))

        for key, value in _flatten_toc(children, depth + 1).items():
            flat.setdefault(key, value)
    return flat


def _heading_from_markup(soup: BeautifulSoup) -> tuple[str | None, int | None]:
    """Fall back to the document's own first heading tag and its level.

    Used when the table of contents has nothing to say about a spine item,
    which is common for front matter and for books with a sparse NCX.
    """
    for level in range(1, 7):
        element = soup.find(f"h{level}")
        if element is not None and (text := element.get_text(strip=True)):
            return text, level
    return None, None
