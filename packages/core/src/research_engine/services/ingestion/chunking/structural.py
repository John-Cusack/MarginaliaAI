"""Structural chunker — respects document structural decomposition."""

from __future__ import annotations

from research_engine.domain.errors import ChunkingError
from research_engine.domain.passages import PassageDraft
from research_engine.services.ingestion.chunking.fixed_window import trim_span
from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker


def _approx_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token, matching the other chunkers."""
    return max(1, len(text) // 4)


class StructuralChunker:
    id = "structural"
    # 3.0: sections longer than `max_tokens` are split into prose windows.
    # Passage boundaries change, so 2.0 passages are stale — see `reindex`.
    version = "3.0"

    def __init__(self, max_tokens: int = 500, overlap_tokens: int = 50) -> None:
        #: A section is a unit of authorship, not of retrieval. A book chapter
        #: runs to thousands of tokens, and emitting it whole would produce
        #: passages an order of magnitude larger than every other chunker's,
        #: diluting their embeddings and blunting the search that reads them.
        #: Sections stay the addressing unit; oversized ones are windowed.
        self._max_tokens = max_tokens
        self._windows = ProseWindowChunker(max_tokens, overlap_tokens)

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

            for draft in await self._drafts_for_section(
                text, start, locator, section_meta
            ):
                chunks.append(draft.model_copy(update={"position": position}))
                position += 1

        return chunks

    async def _drafts_for_section(
        self, text: str, start: int, locator: dict, section_meta: dict
    ) -> list[PassageDraft]:
        """One passage for a section that fits; prose windows for one that does not.

        Window offsets come back relative to the section, so they are shifted by
        the section's own start. That keeps the contract every passage owes —
        ``canonical_text[char_start:char_end] == text`` — true of the pieces as
        it was of the whole.
        """
        if _approx_tokens(text) <= self._max_tokens:
            return [
                PassageDraft(
                    position=0,
                    char_start=start,
                    char_end=start + len(text),
                    text=text,
                    token_count=_approx_tokens(text),
                    chunker=self.id,
                    chunker_version=self.version,
                    metadata=section_meta,
                    locator=locator,
                )
            ]

        windows = await self._windows.chunk(text, section_meta)
        return [
            window.model_copy(
                update={
                    "char_start": start + window.char_start,
                    "char_end": start + window.char_end,
                    "chunker": self.id,
                    "chunker_version": self.version,
                    # The heading travels with every piece: a fragment that has
                    # lost its section is exactly the disconnected chunk this
                    # chunker exists to avoid.
                    "locator": {
                        **locator,
                        "section_part": index + 1,
                        "section_parts": len(windows),
                    },
                }
            )
            for index, window in enumerate(windows)
        ]

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
