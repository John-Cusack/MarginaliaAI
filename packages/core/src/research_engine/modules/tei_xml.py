"""TEI XML ingestion module using lxml."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()

_TEI_NAMESPACE = "http://www.tei-c.org/ns/1.0"
_NS = {"tei": _TEI_NAMESPACE}


class TEIXMLModule:
    id = "tei_xml"
    version = "1.0"
    supported_extensions = {".xml", ".tei"}
    supported_mime_types = {"application/xml", "text/xml", "application/tei+xml"}

    async def detect(self, source_path: Path) -> tuple[float, str]:
        """Detect whether the source is a TEI XML file."""
        suffix = source_path.suffix.lower()
        if suffix not in self.supported_extensions:
            return 0.0, f"extension '{suffix}' does not match XML"

        # Must peek at content to confirm TEI namespace
        try:
            loop = asyncio.get_event_loop()
            head = await loop.run_in_executor(None, self._read_head, source_path)
            if _TEI_NAMESPACE in head:
                return 0.95, "file contains TEI namespace declaration"
            if "<TEI" in head:
                return 0.7, "file contains <TEI> root element"
        except Exception:
            pass

        return 0.0, "not detected as TEI XML"

    async def parse(self, source_path: Path) -> tuple[str, str, dict]:
        """Parse a TEI XML file and extract structured text."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._extract, source_path)

    def default_chunker(self) -> str:
        return "structural"

    def default_document_type(self) -> str:
        return "scholarly"

    @staticmethod
    def _read_head(path: Path, size: int = 4096) -> str:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(size)

    @staticmethod
    def _extract(source_path: Path) -> tuple[str, str, dict]:
        from lxml import etree

        tree = etree.parse(str(source_path))  # noqa: S320
        root = tree.getroot()

        # Handle namespaced and non-namespaced TEI
        ns = _NS if root.tag.startswith("{") else {}
        prefix = "tei:" if ns else ""

        def _find(el: etree._Element, xpath: str) -> etree._Element | None:
            return el.find(xpath, ns)

        def _findall(el: etree._Element, xpath: str) -> list:
            return el.findall(xpath, ns)

        def _text_content(el: etree._Element | None) -> str:
            if el is None:
                return ""
            return "".join(el.itertext()).strip()

        # Extract title from teiHeader
        header = _find(root, f"{prefix}teiHeader")
        title = ""
        author = ""
        date = ""

        if header is not None:
            title_el = _find(
                header,
                f".//{prefix}titleStmt/{prefix}title",
            )
            title = _text_content(title_el)

            author_el = _find(
                header,
                f".//{prefix}titleStmt/{prefix}author",
            )
            author = _text_content(author_el)

            date_el = _find(
                header,
                f".//{prefix}publicationStmt/{prefix}date",
            )
            if date_el is not None:
                date = date_el.get("when", "") or _text_content(date_el)

        if not title:
            title = source_path.stem

        # Extract body text
        body = _find(root, f".//{prefix}body")
        if body is None:
            # Try text element
            body = _find(root, f".//{prefix}text")

        sections: list[str] = []
        if body is not None:
            # Walk div elements for structured extraction
            divs = _findall(body, f".//{prefix}div")
            if divs:
                for div in divs:
                    section_text = _text_content(div)
                    if section_text:
                        sections.append(section_text)
            else:
                # Fall back to full body text
                body_text = _text_content(body)
                if body_text:
                    sections.append(body_text)

        full_text = "\n\n".join(sections)

        # Count structural elements
        div_count = len(_findall(root, f".//{prefix}div"))
        note_count = len(_findall(root, f".//{prefix}note"))
        bibl_count = len(_findall(root, f".//{prefix}bibl"))

        metadata: dict = {
            "char_count": len(full_text),
            "section_count": len(sections),
            "div_count": div_count,
            "file_name": source_path.name,
            "format": "tei_xml",
        }
        if author:
            metadata["author"] = author
        if date:
            metadata["date"] = date
        if note_count:
            metadata["note_count"] = note_count
        if bibl_count:
            metadata["bibliography_count"] = bibl_count

        return full_text, title, metadata
