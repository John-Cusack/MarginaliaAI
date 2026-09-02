"""Ingestion pipeline stage coordination."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog

from research_engine.domain.documents import DocumentDraft
from research_engine.services.ingestion.chunking.fixed_window import FixedWindowChunker
from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker
from research_engine.services.ingestion.chunking.structural import StructuralChunker
from research_engine.services.ingestion.chunking.whole_or_paragraph import WholeOrParagraphChunker

if TYPE_CHECKING:
    from pathlib import Path

    from research_engine.domain.passages import PassageDraft

logger = structlog.get_logger()

#: Counts a parser reports about its own structure. Non-zero here with an
#: empty section table means the structure was found and then dropped.
_STRUCTURE_COUNTS = ("heading_count", "section_count", "chapter_count", "div_count")


CORE_CHUNKERS: dict[str, type] = {
    "prose_window": ProseWindowChunker,
    "structural": StructuralChunker,
    "whole_or_paragraph": WholeOrParagraphChunker,
    "fixed_window": FixedWindowChunker,
}


def current_chunker_versions() -> dict[str, str]:
    """Chunker id to the version currently emitted, core plus plugin.

    Used to find passages written by a superseded chunker, which are the ones
    `reindex chunks` has to re-anchor.
    """
    versions = {cid: cls.version for cid, cls in CORE_CHUNKERS.items()}

    from research_engine.plugins.registry import _global_registry

    if _global_registry is not None:
        for cid, factory in _global_registry._chunkers.items():
            chunker = factory() if isinstance(factory, type) else factory
            version = getattr(chunker, "version", None)
            if version is not None:
                versions[cid] = version
    return versions


def get_chunker(chunker_id: str) -> object:
    """Resolve a chunker by ID."""
    chunkers = CORE_CHUNKERS
    cls = chunkers.get(chunker_id)
    if cls is None:
        # Check plugin-registered chunkers
        from research_engine.plugins.registry import _global_registry

        if _global_registry is not None:
            try:
                cls = _global_registry.resolve_chunker(chunker_id)
            except Exception:
                cls = None
    if cls is None:
        raise ValueError(f"Unknown chunker: {chunker_id}")
    return cls() if isinstance(cls, type) else cls


def _reported_structure(metadata: dict) -> dict[str, int]:
    """Structure the parser says it found, whether or not it handed any over."""
    return {
        key: value
        for key in _STRUCTURE_COUNTS
        if isinstance(value := metadata.get(key), int) and value > 0
    }


async def run_chunking(
    text: str,
    chunker_id: str,
    metadata: dict | None = None,
    *,
    parser_id: str | None = None,
) -> list[PassageDraft]:
    """Run a chunker on text and return passage drafts.

    The structural chunker needs the parser's section decomposition, which
    arrives in ``metadata["sections"]`` as offset ranges into *text*. When a
    parser supplies none — plain text, or a format with no structure to
    recover — prose windows are the honest fallback rather than a failure.

    That fallback is also how three built-in modules shipped for months
    declaring ``structural`` and handing over nothing: the demotion is invisible
    in the result, because prose windows are perfectly good passages. So it is
    announced. A document that genuinely has no headings is reported at info; a
    parser that counted structure and then dropped it is a defect, and says so.
    """
    chunker = get_chunker(chunker_id)

    if chunker_id == "structural":
        sections = (metadata or {}).get("sections")
        if not sections:
            _report_demotion(metadata or {}, parser_id)
            return await ProseWindowChunker().chunk(text, metadata)
        # The section table addresses this document as a whole; repeating it in
        # every passage's metadata would store it once per passage.
        passage_metadata = {
            key: value for key, value in (metadata or {}).items() if key != "sections"
        }
        return await chunker.chunk(sections, passage_metadata, full_text=text)

    return await chunker.chunk(text, metadata)


def _report_demotion(metadata: dict, parser_id: str | None) -> None:
    """Say that a document asked for structural chunking and did not get it."""
    common = {
        "parser": parser_id,
        "file": metadata.get("file_name"),
        "chunker": "prose_window",
    }
    if found := _reported_structure(metadata):
        logger.warning(
            "structural_sections_missing",
            **common,
            reported=found,
            detail=(
                "The parser counted structure and then supplied no "
                "metadata['sections'], so it was chunked into prose windows. "
                "Its headings are not addressable and its node tree will be a "
                "bare root. The parser needs to emit a section table."
            ),
        )
        return
    logger.info(
        "structural_sections_absent",
        **common,
        detail=(
            "No sections to chunk on, so prose windows were used. Expected for "
            "a document with no headings; if this format always has them, the "
            "parser is not reporting them."
        ),
    )


def build_document_draft(
    source_path: Path,
    title: str | None,
    document_type: str,
    parser_id: str,
    parser_version: str,
    metadata: dict,
    language: str | None = None,
) -> DocumentDraft:
    """Build a DocumentDraft from parsed data."""
    content_hash = hashlib.sha256(source_path.read_bytes()).digest()
    return DocumentDraft(
        title=title,
        document_type=document_type,
        language=language,
        source=str(source_path.resolve()),
        content_hash=content_hash,
        parser=parser_id,
        parser_version=parser_version,
        metadata=metadata,
    )
