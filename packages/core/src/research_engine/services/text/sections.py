"""Recover a section table from markdown headings.

Docling converts PDFs, DOCX and the rest by exporting a `DoclingDocument` to
markdown, and its headings survive that export as ordinary `#` lines. That is
the cheap seam: rather than walking the `DoclingDocument` tree and then hunting
each node's prose in the exported string, read the structure back out of the
markdown that is already the canonical text. The offsets are then exact by
construction, because they are offsets into the very string being scanned.

The output is the same section table an EPUB produces — boundaries, heading and
level — so every consumer downstream is indifferent to which parser it came
from.
"""

from __future__ import annotations

import re

from research_engine.services.ingestion.chunking.fixed_window import trim_span

#: ATX headings only. Setext (`===` underlines) is not emitted by Docling and is
#: ambiguous with horizontal rules and table borders, which would cost more in
#: false sections than it could recover in real ones.
_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*$", re.MULTILINE)


def sections_from_markdown(text: str) -> list[dict]:
    """Section boundaries for each markdown heading in *text*, in document order.

    A section runs from its own heading line to the start of the next heading of
    *any* level, or to the end of the text. Sections are therefore disjoint: a
    chapter's span holds only the prose before its first subsection, not the
    subsections themselves. That is deliberate — it reports what the author put
    under each heading, and ``build_node_tree`` widens parents to cover their
    descendants once nesting is known from ``level``. Splitting the two keeps
    this function a straight read of the text, with no view about hierarchy.

    Prose before the first heading gets no section. It is not lost — the root
    node of a document tree spans the whole text — and inventing an untitled
    sibling for it would put a node with nothing to say beside real chapters.
    """
    matches = list(_HEADING.finditer(text))
    if not matches:
        return []

    sections: list[dict] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        start, end = trim_span(text, start, end)
        if start >= end:
            continue
        sections.append(
            {
                "char_start": start,
                "char_end": end,
                "heading": match.group(2).strip(),
                "level": len(match.group(1)),
            }
        )
    return sections
