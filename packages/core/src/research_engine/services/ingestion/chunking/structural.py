"""Structural chunker — respects document structural decomposition."""

from __future__ import annotations

from research_engine.domain.errors import ChunkingError
from research_engine.domain.passages import PassageDraft
from research_engine.services.ingestion.chunking.fixed_window import trim_span


class StructuralChunker:
    id = "structural"
    # 2.0: emits char offsets into the document's canonical text.
    version = "2.0"

    async def chunk(
        self,
        sections: list[dict],
        metadata: dict | None = None,
        full_text: str | None = None,
    ) -> list[PassageDraft]:
        """Chunk based on structural sections from the parser.

        Each section dict should have: text, heading (optional), level (optional).

        Offsets come from the section itself when the parser records
        ``char_start`` / ``char_end``. Otherwise *full_text* must be supplied and
        each section's text is located within it, scanning forward so that
        repeated headings resolve to successive occurrences rather than all to
        the first.

        A section that records offsets may omit ``text`` entirely; it is read
        back from *full_text*. That lets a parser hand over a section table of
        pure boundaries, which can be stored on the document without keeping a
        second copy of its prose.
        """
        chunks: list[PassageDraft] = []
        cursor = 0
        position = 0

        for section in sections:
            raw = self._section_text(section, full_text)
            if not raw.strip():
                continue

            start, end, text = self._locate(section, raw, full_text, cursor)
            cursor = end

            locator = {}
            if heading := section.get("heading"):
                locator["heading"] = heading
            if level := section.get("level"):
                locator["level"] = level
            if page := section.get("page"):
                locator["page"] = page

            section_meta = dict(metadata or {})
            if heading := section.get("heading"):
                section_meta["section_heading"] = heading

            chunks.append(
                PassageDraft(
                    position=position,
                    char_start=start,
                    char_end=end,
                    text=text,
                    token_count=max(1, len(text) // 4),
                    chunker=self.id,
                    chunker_version=self.version,
                    metadata=section_meta,
                    locator=locator,
                )
            )
            position += 1

        return chunks

    @staticmethod
    def _section_text(section: dict, full_text: str | None) -> str:
        """The section's prose, read back from *full_text* when not carried."""
        if (text := section.get("text")) is not None:
            return text
        start, end = section.get("char_start"), section.get("char_end")
        if full_text is not None and start is not None and end is not None:
            return full_text[start:end]
        return ""

    @staticmethod
    def _locate(
        section: dict, raw: str, full_text: str | None, cursor: int
    ) -> tuple[int, int, str]:
        if (start := section.get("char_start")) is not None and (
            end := section.get("char_end")
        ) is not None:
            if full_text is not None and full_text[start:end] != raw:
                raise ChunkingError(
                    f"Section reports span ({start}, {end}) but the text there "
                    f"does not match the section text."
                )
            return start, end, raw

        if full_text is None:
            raise ChunkingError(
                "Structural chunking needs offsets: supply full_text, or give "
                "each section char_start and char_end. Passages without a true "
                "span cannot be cited or re-anchored."
            )

        found = full_text.find(raw, cursor)
        if found < 0:
            found = full_text.find(raw)
        if found < 0:
            raise ChunkingError(
                f"Section text not found in the document: {raw[:80]!r}"
            )
        start, end = trim_span(full_text, found, found + len(raw))
        return start, end, full_text[start:end]
