"""Unified document conversion module using Docling."""

from __future__ import annotations

import asyncio
import math
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING

import structlog

from research_engine.services.text.sections import sections_from_markdown

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


def _convert_page_range(
    source_path_str: str, start: int, end: int, *, ocr: bool, device: str = "cpu"
) -> str:
    """Convert a page range of a PDF in a worker process."""
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
    return result.document.export_to_markdown()


def _convert_parallel(source_path: Path, *, ocr: bool, total_pages: int) -> str:
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

    return "\n\n".join(chunks)


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
    version = "1.0"

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
            full_text = _convert_parallel(
                source_path, ocr=needs_ocr, total_pages=total_pages
            )
        else:
            converter = _get_converter(ocr=needs_ocr)
            result = converter.convert(str(source_path))
            full_text = result.document.export_to_markdown()

        title = _extract_title(full_text, source_path)

        metadata: dict = {
            "file_name": source_path.name,
            "char_count": len(full_text),
            "parser": "docling",
            "ocr_applied": needs_ocr,
            # Docling's headings survive the markdown export, so the structure
            # is recoverable from the canonical text itself — no second pass
            # over the DoclingDocument, and offsets exact by construction.
            "sections": sections_from_markdown(full_text),
        }
        if total_pages:
            metadata["page_count"] = total_pages

        # Remove empty values
        metadata = {k: v for k, v in metadata.items() if v not in ("", None)}

        return full_text, title, metadata
