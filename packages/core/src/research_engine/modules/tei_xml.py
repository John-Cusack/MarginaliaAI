"""TEI XML ingestion module using lxml."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from research_engine.services.ingestion.chunking.fixed_window import trim_span

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()

_TEI_NAMESPACE = "http://www.tei-c.org/ns/1.0"
_NS = {"tei": _TEI_NAMESPACE}


def _local(tag: object) -> str:
    """An element's name without its namespace."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _inline_text(element) -> str:
    """One block's text, with inline markup joined by spaces rather than welded.

    `"".join(el.itertext())` ran `<head>Chapter A</head><p>Alpha body.</p>`
    together as `Chapter AAlpha body.`, which is not a word either side of the
    seam and would be embedded and searched as one.
    """
    return " ".join(fragment.strip() for fragment in element.itertext() if fragment.strip())


def _own_text(div) -> str:
    """A div's own prose, excluding nested divs.

    Nested divs get sections of their own, so including their text here
    duplicated it: a two-chapter part stored each chapter twice, once under the
    part and once under itself, because `.//div` matches at every depth while
    `itertext()` already descends.
    """
    pieces: list[str] = []
    if div.text and div.text.strip():
        pieces.append(div.text.strip())
    for child in div:
        if _local(child.tag) != "div" and (fragment := _inline_text(child)):
            pieces.append(fragment)
        if child.tail and child.tail.strip():
            pieces.append(child.tail.strip())
    return "\n".join(pieces)


def _body_sections(body) -> tuple[str, list[dict]]:
    """Canonical text and a section table, walking divs in document order.

    A section holds its div's own prose and stops at its first nested div, so
    sections are disjoint and `build_node_tree` widens parents over their
    children from `level` — the same contract `sections_from_markdown` keeps.
    """
    parts: list[str] = []
    marks: list[dict] = []
    length = 0

    def walk(parent, depth: int) -> None:
        nonlocal length
        for child in parent:
            if _local(child.tag) != "div":
                continue
            own = _own_text(child)
            head = next((el for el in child if _local(el.tag) == "head"), None)
            marks.append(
                {
                    "char_start": length + (2 if parts else 0),
                    "heading": _inline_text(head) if head is not None else None,
                    "level": depth,
                }
            )
            if own:
                if parts:
                    length += 2  # the blank line that will join this section on
                parts.append(own)
                length += len(own)
            walk(child, depth + 1)

    walk(body, 1)
    full_text = "\n\n".join(parts)

    sections: list[dict] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1]["char_start"] if index + 1 < len(marks) else len(full_text)
        start, end = trim_span(full_text, mark["char_start"], end)
        if start >= end:
            continue
        sections.append({k: v for k, v in {**mark, "char_start": start, "char_end": end}.items() if v is not None})
    return full_text, sections


class TEIXMLModule:
    id = "tei_xml"
    # 2.0: nested divs no longer have their text stored twice, and blocks are
    # separated rather than welded together. Canonical text moves, so 1.0
    # documents are stale — there are none.
    version = "2.0"
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

        sections: list[dict] = []
        full_text = ""
        if body is not None:
            full_text, sections = _body_sections(body)
            if not full_text:
                # No divs at all. One section is still better than none: it is
                # what the whole document says, and the root node needs a span.
                full_text = _inline_text(body)

        # Count structural elements
        div_count = len(_findall(root, f".//{prefix}div"))
        note_count = len(_findall(root, f".//{prefix}note"))
        bibl_count = len(_findall(root, f".//{prefix}bibl"))

        metadata: dict = {
            "char_count": len(full_text),
            "section_count": len(sections),
            # Boundaries only, addressed into the canonical text. This module
            # has always declared `structural` as its chunker and handed it
            # nothing, so it silently got prose windows instead.
            "sections": sections,
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
