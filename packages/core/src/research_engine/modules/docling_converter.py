"""Unified document conversion module using Docling."""

from __future__ import annotations

import asyncio
import math
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()

# Formats where Docling excels (AI-powered layout analysis)
_HIGH_CONFIDENCE_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}

# Formats Docling supports but where simpler parsers are adequate
_MEDIUM_CONFIDENCE_EXTS = {
    ".html", ".htm", ".md", ".csv",
    ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp",
    ".tex",
}

_ALL_SUPPORTED_EXTS = _HIGH_CONFIDENCE_EXTS | _MEDIUM_CONFIDENCE_EXTS

_DOCTYPE_BY_EXT = {
    ".xlsx": "spreadsheet",
    ".csv": "spreadsheet",
    ".pptx": "presentation",
}

# Minimum pages per worker before parallelism is worthwhile (~10s startup cost
# per worker for model loading). Also the threshold to enable parallelism at all.
_MIN_PAGES_PER_WORKER = 20

# Estimated peak memory per worker process: ~766MB for model loading + page buffers
# during processing. Measured empirically at ~1.5–2GB peak per worker.
_WORKER_MEMORY_MB = 2048

# Always leave this much memory free for the OS, main process, and breathing room.
_RESERVED_MEMORY_MB = 4096

# Reuse a single converter per pipeline config to avoid reloading models.
_converter_lock = threading.Lock()
_converters: dict[str, object] = {}


def _configured_device() -> str:
    """Read the user-configured device from RE_DOCLING_DEVICE env var.

    Values: "cpu" (default, safe), "auto" (use GPU if available), "cuda" (force GPU).
    Set RE_DOCLING_DEVICE=auto or RE_DOCLING_DEVICE=cuda to enable GPU acceleration
    for small documents processed in a single process.
    """
    return os.environ.get("RE_DOCLING_DEVICE", "cpu")


def _build_pipeline_options(*, ocr: bool, device: str = "auto") -> object:
    """Build PdfPipelineOptions with performance tuning."""
    from docling.datamodel.pipeline_options import (
        AcceleratorOptions,
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
    )

    return PdfPipelineOptions(
        accelerator_options=AcceleratorOptions(
            device=device,
            num_threads=4,
        ),
        do_ocr=ocr,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.FAST,
        ),
        # Skip enrichment features we don't need
        do_code_enrichment=False,
        do_formula_enrichment=False,
        do_picture_description=False,
        do_picture_classification=False,
        generate_page_images=False,
        generate_picture_images=False,
        # Larger batches for the threaded pipeline
        ocr_batch_size=8,
        layout_batch_size=8,
        table_batch_size=8,
    )


def _get_converter(*, ocr: bool) -> object:
    """Return a cached DocumentConverter for the given OCR setting."""
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    cache_key = f"ocr={ocr}"
    with _converter_lock:
        if cache_key in _converters:
            return _converters[cache_key]

    device = _configured_device()
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=_build_pipeline_options(ocr=ocr, device=device),
            ),
        }
    )

    with _converter_lock:
        _converters[cache_key] = converter

    return converter


def _pdf_page_count(source_path: Path) -> int:
    """Return the number of pages in a PDF."""
    try:
        import fitz

        doc = fitz.open(str(source_path))
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception:
        return 0


def _pdf_has_text(source_path: Path, sample_pages: int = 3) -> bool:
    """Check whether a PDF has an embedded text layer (not scanned)."""
    try:
        import fitz

        doc = fitz.open(str(source_path))
        try:
            pages_to_check = min(sample_pages, doc.page_count)
            for i in range(pages_to_check):
                text = doc[i].get_text("text").strip()
                if len(text) > 50:
                    return True
            return False
        finally:
            doc.close()
    except Exception:
        # If pymupdf isn't available or fails, assume text-based (skip OCR)
        return True


#: How items are joined into canonical text. Matches what `export_to_markdown`
#: produces closely enough that the two differ by a trailing newline: measured
#: on a real PDF, 19,665 characters against 19,667.
_ITEM_SEPARATOR = "\n\n"

#: Docling labels that mark a heading. Everything else is body text, and a
#: section per item would turn a 600-page book into forty thousand nodes.
_HEADING_LABELS = frozenset({"section_header", "title"})

#: Docling's own label for a table of contents. A book's front matter is, by
#: definition, what comes before one — which is the only signal available here.
#: `content_layer` does not help: Docling marks a dedication and a chapter alike
#: as `ContentLayer.BODY`, with no furniture classification at all.
_INDEX_LABEL = "document_index"


def _item_markdown(item: object, doc: object) -> str:
    """One item's markdown, falling back to its plain text."""
    try:
        return item.export_to_markdown(doc)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - an item we cannot serialise is not fatal
        return str(getattr(item, "text", "") or "")


def _item_page(item: object) -> int | None:
    """The page an item sits on, from its first provenance record."""
    prov = getattr(item, "prov", None)
    return prov[0].page_no if prov else None


def _text_and_structure(doc: object) -> tuple[str, list[dict], list[dict]]:
    """Canonical text, its section table, and its page boundaries — in one pass.

    Structure used to be recovered by exporting markdown and running a heading
    regex back over it. That is cheap and it caps the structure layer at whatever
    survives the export: Docling writes every heading as `##`, so a 2.9M-character
    book became 213 flat siblings, and page provenance — which Docling records for
    every single item — was discarded entirely, leaving PDF locators at 0%.

    Walking the item stream instead makes offsets exact by construction rather
    than recovered, which is what `EPUBModule` already does with the spine.

    Returns `(text, sections, pages)`. *pages* is a boundary table rather than a
    per-section field because a section spanning pages 42-45 has no single page;
    the section's own starting page is carried too, since that is what
    `StructuralChunker` reads today.
    """
    parts: list[str] = []
    sections: list[dict] = []
    pages: list[dict] = []
    cursor = 0
    last_page: int | None = None
    first_index_at: int | None = None

    for item, _depth in doc.iterate_items():  # type: ignore[attr-defined]
        markdown = _item_markdown(item, doc)
        if not markdown.strip():
            continue

        start = cursor
        parts.append(markdown)
        cursor += len(markdown) + len(_ITEM_SEPARATOR)

        page = _item_page(item)
        if page is not None and page != last_page:
            pages.append({"char_start": start, "page": page})
            last_page = page

        label = str(getattr(item, "label", "") or "")
        short_label = label.rsplit(".", 1)[-1]
        if short_label == _INDEX_LABEL and first_index_at is None:
            first_index_at = start
        if short_label in _HEADING_LABELS:
            sections.append(
                {
                    "char_start": start,
                    # Provisional: extended to the next heading below, so a
                    # section holds its prose and not just its own title.
                    "char_end": start + len(markdown),
                    "heading": str(getattr(item, "text", "") or "").strip() or None,
                    "level": getattr(item, "level", None) or 1,
                    "page": page,
                    "label": label,
                }
            )

    text = _ITEM_SEPARATOR.join(parts)
    sections = _merge_adjacent_headings(sections, text)
    sections = _drop_front_matter(sections, first_index_at)
    for index, section in enumerate(sections):
        following = sections[index + 1]["char_start"] if index + 1 < len(sections) else len(text)
        section["char_end"] = max(section["char_end"], following)
    return text, sections, pages


def _merge_adjacent_headings(sections: list[dict], text: str) -> list[dict]:
    """Join headings separated by nothing but whitespace.

    Layout splits one heading across lines and Docling reports each line as its
    own item: `COMMAND IN THE WESTERN` then `THEATER`, `PART ONE` then
    `Apprenticeship to Arms`. Left alone they become sibling nodes, one of which
    is a fragment. Whether a given heading arrives split is not even stable
    between runs, so this is a repair rather than a preference.
    """
    merged: list[dict] = []
    for section in sections:
        if merged and not text[merged[-1]["char_end"] : section["char_start"]].strip():
            previous = merged[-1]
            titles = [previous.get("heading"), section.get("heading")]
            previous["heading"] = " ".join(t for t in titles if t) or None
            previous["char_end"] = section["char_end"]
            continue
        merged.append(section)
    return merged


def _drop_front_matter(sections: list[dict], first_index_at: int | None) -> list[dict]:
    """Discard headings that precede the table of contents.

    A dedication, a copyright line and a calligrapher's credit are all set like
    headings and all detected as headings, so a passage on page 3 would cite
    itself as belonging to "Donated In Memory Of ROBERT EDWARD PATOW".

    The first `document_index` item is the cut. The *first*, not the last: a
    contents list runs over several pages with headings interleaved, and cutting
    at the last one takes real sections such as `APPENDICES` with it.

    Only the headings are dropped, never the text — it stays in the canonical
    text and simply belongs to the node above. And when no contents page is
    detected there is nothing to cut against, so nothing is dropped.
    """
    if first_index_at is None:
        return sections
    return [s for s in sections if s["char_start"] >= first_index_at]


def _convert_page_range(
    source_path_str: str, start: int, end: int, *, ocr: bool, device: str = "cpu"
) -> tuple[str, list[dict], list[dict]]:
    """Convert a page range of a PDF in a worker process.

    Returns the same triple as `_text_and_structure`. Sending that back rather
    than the `DoclingDocument` itself keeps the pickle small — the document
    carries every bounding box for every item — while losing nothing the caller
    uses. Docling numbers pages absolutely, so the page table needs no shifting;
    only the character offsets do.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=_build_pipeline_options(ocr=ocr, device=device),
            ),
        }
    )
    result = converter.convert(source_path_str, page_range=(start, end))
    return _text_and_structure(result.document)


def _convert_parallel(
    source_path: Path, *, ocr: bool, total_pages: int
) -> tuple[str, list[dict], list[dict]]:
    """Split a large PDF into chunks and process in parallel CPU workers."""
    num_workers = min(
        _default_workers(), math.ceil(total_pages / _MIN_PAGES_PER_WORKER)
    )
    chunk_size = math.ceil(total_pages / num_workers)

    ranges: list[tuple[int, int, str]] = []  # (start, end, device)
    for i in range(num_workers):
        start = i * chunk_size + 1  # 1-indexed
        end = min((i + 1) * chunk_size, total_pages)
        if start <= end:
            # CPU for parallel workers — forked processes can't share GPU VRAM
            # effectively. The single-process path uses GPU via device="auto".
            ranges.append((start, end, "cpu"))

    logger.info(
        "docling_parallel_convert",
        file=source_path.name,
        total_pages=total_pages,
        workers=len(ranges),
        pages_per_worker=chunk_size,
    )

    path_str = str(source_path)
    with ProcessPoolExecutor(max_workers=len(ranges)) as pool:
        futures = [
            pool.submit(
                _convert_page_range, path_str, start, end,
                ocr=ocr, device=device,
            )
            for start, end, device in ranges
        ]
        chunks = [f.result() for f in futures]

    return _join_chunks(chunks)


def _join_chunks(
    chunks: list[tuple[str, list[dict], list[dict]]],
) -> tuple[str, list[dict], list[dict]]:
    """Concatenate worker results, shifting each one's offsets onto the whole.

    The same arithmetic a book assembled from articles needs: offsets are
    relative to the piece they were measured in, and they have to address the
    document they end up in. Page numbers are already absolute.
    """
    parts: list[str] = []
    sections: list[dict] = []
    pages: list[dict] = []
    cursor = 0

    for text, chunk_sections, chunk_pages in chunks:
        for section in chunk_sections:
            sections.append(
                {
                    **section,
                    "char_start": section["char_start"] + cursor,
                    "char_end": section["char_end"] + cursor,
                }
            )
        for page in chunk_pages:
            pages.append({**page, "char_start": page["char_start"] + cursor})
        parts.append(text)
        cursor += len(text) + len(_ITEM_SEPARATOR)

    joined = _ITEM_SEPARATOR.join(parts)
    # A section that ran to the end of its own chunk should run to the start of
    # the next chunk's first section instead, or the prose between them belongs
    # to no section at all.
    for index, section in enumerate(sections):
        following = (
            sections[index + 1]["char_start"]
            if index + 1 < len(sections)
            else len(joined)
        )
        section["char_end"] = max(section["char_end"], following)
    return joined, sections, pages


def _default_workers() -> int:
    """Scale workers with available cores, capped by available memory."""
    cpu_count = os.cpu_count() or 4
    max_by_cpu = cpu_count - 2

    try:
        available_mb = _available_memory_mb()
    except Exception:
        available_mb = 8192  # conservative fallback: assume 8GB

    # Use at most half the available memory for workers — leaves headroom for
    # page buffers, kernel caches, and other processes during processing.
    usable_mb = max(0, available_mb - _RESERVED_MEMORY_MB) // 2
    max_by_memory = int(usable_mb / _WORKER_MEMORY_MB)

    workers = max(2, min(max_by_cpu, max_by_memory))
    logger.debug(
        "docling_worker_scaling",
        cpu_count=cpu_count,
        available_memory_mb=available_mb,
        max_by_cpu=max_by_cpu,
        max_by_memory=max_by_memory,
        workers=workers,
    )
    return workers


def _available_memory_mb() -> int:
    """Get available system memory in MB from /proc/meminfo (Linux)."""
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                # Format: "MemAvailable:   12345678 kB"
                return int(line.split()[1]) // 1024
    msg = "MemAvailable not found in /proc/meminfo"
    raise OSError(msg)


def _extract_title(full_text: str, source_path: Path) -> str:
    """Extract a title from the converted text or fall back to filename."""
    for line in full_text.splitlines():
        stripped = line.strip()
        # Skip image placeholders and blank lines
        if not stripped or stripped.startswith("<!-- "):
            continue
        stripped = stripped.lstrip("#").strip()
        if stripped and len(stripped) <= 300:
            return stripped
    return source_path.stem


class DoclingModule:
    id = "docling"
    # 2.0: canonical text is built from Docling's item stream rather than its
    # markdown export, so structure and page provenance survive. The text moves
    # by a trailing newline and the offsets move with it — a re-ingest, not a
    # re-chunk. 1.0 documents are stale.
    version = "2.0"

    async def detect(self, source_path: Path) -> tuple[float, str]:
        """Return high confidence for formats Docling handles well."""
        suffix = source_path.suffix.lower()
        if suffix in _HIGH_CONFIDENCE_EXTS:
            return 0.95, f"Docling excels at '{suffix}' format"
        if suffix in _MEDIUM_CONFIDENCE_EXTS:
            return 0.85, f"Docling supports '{suffix}' format"
        return 0.0, "unsupported format for Docling"

    async def parse(self, source_path: Path) -> tuple[str, str, dict]:
        """Convert document via Docling and return (text, title, metadata)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._convert, source_path)

    def default_chunker(self) -> str:
        return "structural"

    def default_document_type(self) -> str:
        return "generic"

    def default_document_type_for(self, source_path: Path) -> str:
        """Infer document type from file extension."""
        return _DOCTYPE_BY_EXT.get(source_path.suffix.lower(), "generic")

    @staticmethod
    def _convert(source_path: Path) -> tuple[str, str, dict]:
        is_pdf = source_path.suffix.lower() == ".pdf"

        # Auto-detect whether OCR is needed for PDFs
        needs_ocr = False
        total_pages = 0
        if is_pdf:
            has_text = _pdf_has_text(source_path)
            needs_ocr = not has_text
            total_pages = _pdf_page_count(source_path)
            logger.info(
                "docling_ocr_decision",
                file=source_path.name,
                has_text_layer=has_text,
                ocr_enabled=needs_ocr,
                pages=total_pages,
            )

        # Use parallel processing for large PDFs
        if is_pdf and total_pages > _MIN_PAGES_PER_WORKER:
            full_text, sections, pages = _convert_parallel(
                source_path, ocr=needs_ocr, total_pages=total_pages
            )
        else:
            converter = _get_converter(ocr=needs_ocr)
            result = converter.convert(str(source_path))
            full_text, sections, pages = _text_and_structure(result.document)

        title = _extract_title(full_text, source_path)

        metadata: dict = {
            "file_name": source_path.name,
            "char_count": len(full_text),
            "parser": "docling",
            "ocr_applied": needs_ocr,
            # Read off the item stream, not recovered by a heading regex over
            # the export. Docling writes every heading as `##`, so the regex saw
            # one level however deep the document went, and never saw a page
            # number at all.
            "sections": sections,
            # Offset -> page boundaries for the whole document. Sections carry
            # the page they *start* on, which is what `StructuralChunker` reads;
            # this table is what a span crossing a page break needs, and is the
            # same shape the Logos pack's page markers already use.
            "pages": pages,
        }
        if total_pages:
            metadata["page_count"] = total_pages

        # Remove empty values
        metadata = {k: v for k, v in metadata.items() if v not in ("", None)}

        return full_text, title, metadata
