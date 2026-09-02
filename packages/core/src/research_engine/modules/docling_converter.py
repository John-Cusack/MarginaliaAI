"""Unified document conversion module using Docling."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

from research_engine.domain.errors import describe_exception

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

#: Below this a PDF converts in the calling process. Parallelism costs roughly
#: ten seconds of model loading per worker, which is not worth paying to split a
#: pamphlet. This is only the gate; it no longer doubles as a task size.
#:
#: The real gate is `_wants_parallel` below, which also requires more than one
#: task: a pool of one worker running one range is not parallelism, it is the
#: single-process path with an extra model load and no accelerator.
_MIN_PAGES_FOR_PARALLEL = 20

#: Pages one worker converts in a single task, fixed rather than derived from
#: the worker count.
#:
#: It does **not** bound memory, which is what it was expected to do. Measured on
#: Campaigns of Napoleon, peak RSS is flat in page count — 5,109 MB for pages
#: 1-25, 5,434 for 1-50, 5,359 for 1-100, 5,333 for 1-200 — while two different
#: 25-page ranges differ by 1.9 GB (3,232 MB of plain prose against 5,109 MB of
#: plates and maps). Cost follows content, not volume.
#:
#: What a fixed size does buy: `_WORKER_MEMORY_MB` becomes a property of a task
#: rather than of the document, expensive pages spread across workers instead of
#: landing on one, and a worker that dies costs 50 pages to redo rather than its
#: share of the whole book. 50 gives ~25 tasks for a book-length PDF, which
#: balances across any worker count this will size.
_DEFAULT_PAGES_PER_TASK = 50

#: Peak resident memory to budget for one worker.
#:
#: Sized to the worst peak actually observed, not to a mean or an estimate. Three
#: full conversions of the same 1,224-page book reported 9,828, 9,673 and
#: 8,391 MB; the worker the kernel killed reached 8,045 MB. Single ranges measured
#: in isolation run 3,232-5,434 MB, so a worker's lifetime high-water mark is
#: roughly twice what any one task suggests — budget for the former.
#:
#: Note the 17% spread across runs of the *same* document: which expensive ranges
#: land on which worker is luck, so there is no single right answer to measure
#: towards, only a worst case to stay above.
#:
#: This is deliberately pessimistic: it assumes every worker peaks at once, which
#: measured runs say they do not (6 workers x 9.7 GB predicts 58 GB against an
#: observed 37 GB). Relying on peaks staying staggered is exactly the assumption
#: that fails on a document where every range is expensive, and the failure mode
#: is the OOM killer.
#:
#: The value this replaces was 2,048 MB with no measurement behind it. Since
#: total memory is `workers * this`, being 5x low is the whole difference between
#: finishing and being killed.
_WORKER_MEMORY_MB = 10240

#: Never hand the whole machine to Docling. The OS, Postgres and the parent
#: process all have to keep running while a long conversion is under way.
_RESERVED_MEMORY_MB = 4096

#: How far the recovery ladder will climb down before giving up. Concurrency
#: halves first and task size second, so this has to cover both: eight steps
#: takes 16 workers down to 1 and then a 50-page task down to the floor.
_MAX_RECOVERY_ATTEMPTS = 8

#: The floor on task size. Below this the per-task model load dominates and a
#: conversion that still cannot fit is not going to fit.
_MIN_PAGES_PER_TASK = 5

# Reuse a single converter per pipeline config to avoid reloading models.
_converter_lock = threading.Lock()
_converters: dict[str, object] = {}


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


def _get_converter(*, ocr: bool, device: str) -> object:
    """Return a cached DocumentConverter for the given OCR and device setting.

    The device is part of the cache key. It was not, and the cache was keyed on
    `ocr=` alone, so the first converter built decided the device for every later
    conversion in the process.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    cache_key = f"ocr={ocr},device={device}"
    with _converter_lock:
        if cache_key in _converters:
            return _converters[cache_key]

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


def _starts_past_centre(item: object, doc: object) -> bool:
    """True when an item's left edge sits right of the page midpoint.

    Docling's layout model reads visual salience: an isolated short line is a
    heading whether it is a chapter title or the closing of a letter. So
    `"Yours affectionately Geo B McClellan"` arrives as a `section_header` and
    becomes a node, and passages beneath it cite themselves as belonging to a
    signature.

    Alignment separates the two, and the test can be simple because of how
    alignment works. A left-aligned heading starts at the margin; a centred one
    of width `w` on a page of width `W` starts at `(W - w) / 2`, which is left of
    `W / 2` for any width at all. Only text set to the right — a signature, a
    date line, an attribution — begins past the midpoint. Measured on the
    McClellan papers, every genuine heading starts between 5% and 14% across,
    and the misclassified closing starts at 57%.

    Falls back to False whenever geometry is unavailable, so a document without
    provenance keeps every heading rather than losing them all.
    """
    prov = getattr(item, "prov", None)
    if not prov:
        return False
    bbox = getattr(prov[0], "bbox", None)
    page = getattr(doc, "pages", {}).get(prov[0].page_no)
    size = getattr(page, "size", None)
    width = getattr(size, "width", 0) or 0
    if bbox is None or width <= 0:
        return False
    return bbox.l > width / 2


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
        if short_label in _HEADING_LABELS and not _starts_past_centre(item, doc):
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
) -> tuple[str, list[dict], list[dict], float]:
    """Convert a page range of a PDF in a worker process.

    Returns `_text_and_structure`'s triple plus this worker's peak resident
    memory in MB. Sending the triple back rather than the `DoclingDocument`
    itself keeps the pickle small — the document carries every bounding box for
    every item — while losing nothing the caller uses. Docling numbers pages
    absolutely, so the page table needs no shifting; only the character offsets do.

    The peak is measured rather than modelled because modelling it is what
    failed: a constant nobody checked sized the pool at thirteen workers on a
    machine that could hold five, and the kernel resolved the disagreement.

    Note `ru_maxrss` is the high-water mark for the *process*, and a pool worker
    outlives the task that reports it. So this is what the worker has occupied
    across every range it has handled, not the cost of this range alone — which
    is the number the pool sizing wants, since that is what the machine has to
    hold. It also means a gap between this and the same range measured in a
    fresh process is Docling accumulating across tasks.
    """
    import resource

    # The per-process cache, not a fresh converter each time. Building one per
    # task made every worker pay the model load again per range, which also made
    # a *smaller* `pages_per_task` quietly worse: more tasks per worker means
    # more converters. Measured over a full book it cut steady-state memory from
    # 37.4 GB to 34.0 GB and pulled the spread across non-peak workers from
    # 4,373-6,162 MB down to 3,792-4,073 MB.
    #
    # It does *not* lower the peak — 9,828 MB before, 9,673 MB after. The peak is
    # set by one expensive range, and a lifetime high-water mark cannot be
    # undone by being tidier afterwards. `_WORKER_MEMORY_MB` covers that.
    converter = _get_converter(ocr=ocr, device=device)
    result = converter.convert(source_path_str, page_range=(start, end))
    text, sections, pages = _text_and_structure(result.document)
    # kB on Linux, bytes on macOS. Only Linux is supported here (the memory
    # probe reads /proc/meminfo), so kB it is.
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    return text, sections, pages, peak_mb


class ConversionPlan(NamedTuple):
    """How a PDF will be split, how many workers will run, and why."""

    ranges: list[tuple[int, int]]
    workers: int
    reason: str


def plan_conversion(
    total_pages: int,
    *,
    cpu_count: int,
    available_mb: int,
    per_worker_mb: int,
    pages_per_task: int,
    override: int | None = None,
) -> ConversionPlan:
    """Decide the page ranges and the worker count.

    Pure — no `/proc`, no pool, no clock — because the decision it makes is the
    one that went wrong, and the version it replaces could only be tested by
    running it on the machine whose memory it was misjudging.

    Total memory is `workers * per_worker_mb`, because measured peak per worker
    barely moves with the number of pages in a task. So the worker count is the
    lever that matters, and `per_worker_mb` has to be the worst case rather than
    the typical one — the spread across ranges of the same size is 2.5x.
    """
    pages_per_task = max(_MIN_PAGES_PER_TASK, pages_per_task)
    ranges = [
        (start, min(start + pages_per_task - 1, total_pages))
        for start in range(1, total_pages + 1, pages_per_task)
    ]
    tasks = len(ranges)

    if override is not None:
        workers = max(1, min(override, tasks))
        return ConversionPlan(
            ranges, workers, f"worker count set explicitly to {override}"
        )

    # Leave two cores for the parent and the OS; a fully subscribed box makes the
    # run slower, not faster, because each worker already runs four threads.
    by_cpu = max(1, cpu_count - 2)
    usable_mb = max(0, available_mb - _RESERVED_MEMORY_MB)
    by_memory = max(1, usable_mb // per_worker_mb)
    workers = max(1, min(by_cpu, by_memory, tasks))

    # No floor above one. The previous `max(2, ...)` guaranteed parallelism even
    # on a machine with no memory to spare, which is exactly the machine that
    # cannot afford it. Parallelism is an optimisation; finishing is not.
    return ConversionPlan(
        ranges,
        workers,
        f"{tasks} tasks of {pages_per_task}p; cores allow {by_cpu}, "
        f"memory allows {by_memory} ({usable_mb} MB usable / {per_worker_mb} MB per worker)",
    )


#: One worker's answer for one page range: the usual triple plus its peak RSS.
Converted = tuple[str, list[dict], list[dict], float]


class PoolBroken(Exception):
    """A worker died, carrying the work that survived it.

    One dead worker breaks the executor for everything still pending, but the
    ranges that already finished are still good. In the failure this was written
    for, eleven of twelve workers had completed and every one of them was thrown
    away — eight minutes of conversion discarded to retry a single range.
    """

    def __init__(self, completed: dict[tuple[int, int], Converted], cause: Exception):
        # Say what it means, not what the executor called it. The stock message
        # is "A process in the process pool was terminated abruptly", which
        # names no cause; attributing it the first time took reading `dmesg`.
        super().__init__(
            "A Docling worker was terminated abruptly, which on this path is "
            "almost always the kernel OOM killer reclaiming its memory "
            "(confirm with `dmesg | grep -i 'killed process'`). Lower "
            "RE_DOCLING_MAX_WORKERS or RE_DOCLING_PAGES_PER_TASK if it recurs."
        )
        self.completed = completed
        self.cause = cause


#: How much *more* attractive an OOM victim a worker should be than its parent,
#: on the kernel's -1000..1000 scale. Relative, not absolute: a container may
#: already place the whole process tree well above zero, and an absolute value
#: then expresses no preference at all. Setting 500 inside a tree already at 500
#: is what CI does, and it silently did nothing.
_WORKER_OOM_SCORE_BOOST = 500

#: The kernel's ceiling. Nothing above this is expressible.
_MAX_OOM_SCORE_ADJ = 1000


def _boosted_oom_score(inherited: int) -> int:
    """Where a worker should sit given the score it inherited from its parent.

    Pure, because the interesting cases are environments this machine is not:
    a container starting at 500, a supervisor that has already protected itself
    into negative territory.
    """
    return min(_MAX_OOM_SCORE_ADJ, inherited + _WORKER_OOM_SCORE_BOOST)


def _prefer_killing_this_worker() -> None:
    """Ask the kernel to reap workers before the process supervising them.

    The recovery ladder only helps if there is something left to run it. Left to
    itself the OOM killer picks by badness score, and the parent — holding the
    whole document's text — is a plausible choice; if it goes, nothing retries
    and nothing reports why.

    A forked child inherits its parent's `oom_score_adj`, so reading it here
    reads the parent's, and the boost goes on top. Raising one's own score needs
    no privileges (only lowering it does), and failure is not worth aborting a
    conversion over: it means the previous, unmanaged behaviour, which the ladder
    already had to cope with.
    """
    # `open` rather than pathlib, matching `_available_memory_mb` below.
    # Suppressed: not Linux, or a sandbox that forbids the read or the write.
    with contextlib.suppress(OSError, ValueError):
        with open("/proc/self/oom_score_adj") as handle:
            inherited = int(handle.read().strip())
        with open("/proc/self/oom_score_adj", "w") as handle:
            handle.write(str(_boosted_oom_score(inherited)))


def _run_pool(
    source_path: Path,
    ranges: list[tuple[int, int]],
    *,
    workers: int,
    ocr: bool,
    completed: dict[tuple[int, int], Converted] | None = None,
) -> dict[tuple[int, int], Converted]:
    """Convert every range not already in *completed*, across *workers* processes.

    Returns results keyed by page range rather than a joined document, so a retry
    can pick up where the last attempt died. Raises `PoolBroken` carrying
    whatever finished.
    """
    done: dict[tuple[int, int], Converted] = dict(completed or {})
    path_str = str(source_path)
    submitted: dict[Any, tuple[int, int]] = {}

    try:
        with ProcessPoolExecutor(
            max_workers=workers, initializer=_prefer_killing_this_worker
        ) as pool:
            for start, end in ranges:
                if (start, end) in done:
                    continue
                # CPU for parallel workers — forked processes can't share GPU
                # VRAM effectively. The single-process path uses the configured
                # device.
                submitted[
                    pool.submit(
                        _convert_page_range,
                        path_str, start, end, ocr=ocr, device="cpu",
                    )
                ] = (start, end)
            for future, page_range in submitted.items():
                done[page_range] = future.result()
    except BrokenProcessPool as exc:
        for future, page_range in submitted.items():
            if future.done() and not future.cancelled() and future.exception() is None:
                done[page_range] = future.result()
        raise PoolBroken(done, exc) from exc

    return done


def _assemble(
    ranges: list[tuple[int, int]], results: dict[tuple[int, int], Converted]
) -> tuple[tuple[str, list[dict], list[dict]], float]:
    """Join the per-range results in page order and report the peak.

    Order comes from *ranges*, not from the dict: results accumulate across
    retries and a document assembled in completion order would interleave its
    own chapters.
    """
    ordered = [results[page_range] for page_range in ranges]
    # Max, not sum: this sizes one worker, and the pool holds `workers` of them.
    peak_mb = max((r[3] for r in ordered), default=0.0)
    return _join_chunks([(t, s, p) for t, s, p, _ in ordered]), peak_mb


def _convert_parallel(
    source_path: Path,
    *,
    ocr: bool,
    total_pages: int,
    max_workers: int | None,
    pages_per_task: int,
) -> tuple[str, list[dict], list[dict], dict]:
    """Convert a large PDF in parallel, climbing down when a worker is killed.

    A worker killed by the kernel's OOM reaper surfaces as `BrokenProcessPool`,
    whose message — "A process in the process pool was terminated abruptly" —
    names no cause. Left unhandled it lost a 1,224-page book ten minutes into
    its conversion, and attributing it took reading kernel logs.

    So the failure is treated the way `embed_batches` treats a batch too large
    for the accelerator: reduce and retry rather than abort. Concurrency halves
    first because it cannot change the output; task size halves only once there
    is one worker left and nothing else to give.
    """
    attempts = 0
    workers_override = max_workers
    halvings = 0
    completed: dict[tuple[int, int], Converted] = {}

    while True:
        plan = plan_conversion(
            total_pages,
            cpu_count=_usable_cpus(),
            available_mb=_safe_available_memory_mb(),
            per_worker_mb=_WORKER_MEMORY_MB,
            pages_per_task=pages_per_task,
            override=workers_override,
        )
        logger.info(
            "docling_parallel_convert",
            file=source_path.name,
            total_pages=total_pages,
            workers=plan.workers,
            tasks=len(plan.ranges),
            pages_per_task=pages_per_task,
            detail=plan.reason,
        )
        try:
            results = _run_pool(
                source_path,
                plan.ranges,
                workers=plan.workers,
                ocr=ocr,
                completed=completed,
            )
        except PoolBroken as broken:
            exc = broken.cause
            attempts += 1
            reduced = _reduce(plan.workers, pages_per_task)
            if reduced is None or attempts > _MAX_RECOVERY_ATTEMPTS:
                logger.error(
                    "docling_conversion_failed",
                    file=source_path.name,
                    workers=plan.workers,
                    pages_per_task=pages_per_task,
                    attempts=attempts,
                    error=describe_exception(exc),
                    detail=(
                        "A worker died and there is nothing left to reduce. This "
                        "is usually the kernel OOM killer; check `dmesg` for "
                        "'Killed process'."
                    ),
                )
                raise
            workers_override, next_pages_per_task = reduced
            # Ranges that already converted are still good, but only while the
            # split stays the same. A different task size is a different set of
            # page ranges, and results keyed by the old ones no longer address
            # anything.
            completed = broken.completed if next_pages_per_task == pages_per_task else {}
            pages_per_task = next_pages_per_task
            halvings += 1
            logger.warning(
                "docling_conversion_retry",
                file=source_path.name,
                attempt=attempts,
                workers=plan.workers,
                retry_workers=workers_override,
                pages_per_task=pages_per_task,
                reusing_ranges=len(completed),
                of_ranges=len(plan.ranges),
                detail=(
                    "A worker was terminated abruptly, which on this path almost "
                    "always means the kernel reclaimed its memory. Retrying smaller."
                ),
            )
            continue

        (text, sections, pages), peak_mb = _assemble(plan.ranges, results)
        logger.info(
            "docling_parallel_done",
            file=source_path.name,
            workers=plan.workers,
            peak_worker_mb=round(peak_mb, 1),
            budget_mb=_WORKER_MEMORY_MB,
            detail=(
                "peak_worker_mb is the measurement _WORKER_MEMORY_MB is supposed "
                "to predict; a sustained gap means the constant needs revisiting."
            ),
        )
        return text, sections, pages, {
            "peak_worker_mb": round(peak_mb, 1),
            "workers": plan.workers,
            "pages_per_task": pages_per_task,
            "halvings": halvings,
        }


def _wants_parallel(total_pages: int, pages_per_task: int) -> bool:
    """Whether splitting this PDF across processes is worth doing at all.

    Two conditions, and the second is easy to lose: the document has to be big
    enough to be worth the model loads, *and* it has to split into more than one
    task. A 30-page PDF against a 50-page task size yields a single range, and
    running that in a worker buys nothing while costing a fresh model load and
    the configured accelerator — the parallel path is always CPU.
    """
    return total_pages > max(_MIN_PAGES_FOR_PARALLEL, pages_per_task)


def _reduce(workers: int, pages_per_task: int) -> tuple[int, int] | None:
    """The next rung down: fewer workers, or failing that, smaller tasks.

    Workers first, and not only because changing them cannot change the output:
    it is the rung with the leverage. Total memory is `workers * peak`, while
    peak is nearly flat in task size — 25 pages and 200 pages of the same book
    cost the same. Shrinking tasks is a weak last resort, kept because at one
    worker it is the only thing left and it costs nothing to try.

    Returns None when both are already at their floor, which means the failure
    is not memory pressure and retrying would only cost another conversion.
    """
    if workers > 1:
        return workers // 2, pages_per_task
    if pages_per_task > _MIN_PAGES_PER_TASK:
        return 1, max(_MIN_PAGES_PER_TASK, pages_per_task // 2)
    return None


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


def _usable_cpus() -> int:
    """Cores this process may actually run on.

    `os.cpu_count()` reports the machine's cores whether or not they are ours;
    under `taskset`, a cpuset or a container quota the affinity mask is the real
    answer and can be far smaller.
    """
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover - not Linux
        return os.cpu_count() or 4


def _safe_available_memory_mb() -> int:
    """Available system memory, or a deliberately small guess."""
    try:
        return _available_memory_mb()
    except Exception:  # noqa: BLE001 - an unreadable /proc is not fatal
        # Small on purpose. Guessing high here is how the pool ends up sized for
        # memory the machine does not have.
        return 8192


def _available_memory_mb() -> int:
    """Get available system memory in MB from /proc/meminfo (Linux).

    Note this is the *host's* memory. Inside a container with a memory cgroup
    limit it overstates what is available, and the limit is what the OOM killer
    enforces. Nothing here runs in a container today.
    """
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                # Format: "MemAvailable:   12345678 kB"
                return int(line.split()[1]) // 1024
    msg = "MemAvailable not found in /proc/meminfo"
    raise OSError(msg)


#: Titles an authoring tool wrote because nobody gave it one.
_GENERIC_TITLES = frozenset(
    {
        "untitled", "unknown", "document", "documento", "presentation",
        "powerpoint presentation", "(anonymous)", "anonymous", "no title",
        "title", "new document", "book1", "sheet1",
    }
)
#: "Microsoft Word - report.doc" is the filename, not the title.
_AUTHORING_PREFIX = re.compile(r"^microsoft (word|powerpoint|excel) - ", re.I)
_DOCUMENT_EXTENSION = re.compile(
    r"\.(pdf|docx?|indd|pptx?|odt|rtf|tex|qxd|fm|pages|epub)$", re.I
)
_NUMBERED_DEFAULT = re.compile(r"^(document|doc|file|untitled|book|sheet)\s*\d+$", re.I)
#: An underscore not followed by a space. Real titles use "Letter_ John" where a
#: colon was stripped; machine identifiers use "output_CSantiago_fmlrKYMGCeW3Nj3".
_MACHINE_IDENTIFIER = re.compile(r"_(?!\s)")


def _usable_title(raw: str | None) -> str | None:
    """A PDF `/Title` worth using, or None to fall back.

    Measured on 88 PDFs here: 54 declare a title and 19 of those are junk, so
    the field cannot simply be trusted. Every rule below rejects something real
    from that sample — an authoring default (`Document1`, `(anonymous)`), an
    account number (`1099`, `749537 NCM9JP01`), a filename the tool copied in
    (`Microsoft Word - PFS Editable.doc`, `B87023352[1].pdf`), or an export
    identifier (`output_CSantiago_fmlrKYMGCeW3Nj3`).

    A trailing extension is stripped rather than rejected: `Rape Gang Inquiry
    Report.docx` is a real title wearing one, while `B87023352[1].pdf` fails the
    letter rules once it is off. Rejecting on the extension alone lost the first.
    """
    title = (raw or "").strip()
    if not title:
        return None
    if title.lower() in _GENERIC_TITLES or _NUMBERED_DEFAULT.match(title):
        return None
    if _AUTHORING_PREFIX.match(title):
        return None
    title = _DOCUMENT_EXTENSION.sub("", title).strip()
    if not title or _MACHINE_IDENTIFIER.search(title):
        return None
    letters = sum(character.isalpha() for character in title)
    dense = sum(not character.isspace() for character in title)
    if letters < 3 or letters / dense < 0.5:
        return None
    return title


def _pdf_metadata_title(source_path: Path) -> str | None:
    """The title the PDF states about itself, if it is worth having."""
    try:
        import fitz

        doc = fitz.open(str(source_path))
        try:
            return _usable_title((doc.metadata or {}).get("title"))
        finally:
            doc.close()
    except Exception:
        return None


def _extract_title(full_text: str, source_path: Path) -> str:
    """The document's title: what it declares, then what it looks like.

    The first line of converted text was the only source, and on a scanned book
    that is whatever the layout model happened to read first — a library stamp
    (`CENTRAL`), a torn word (`fies`), a fragment of a plate caption (`Mfo mm`).
    All three are real titles from this corpus, and all three PDFs stated their
    correct title in metadata that nothing looked at.

    So metadata first when it is usable, the first line when it is not, and the
    filename when there is no text at all. The first line stays in the chain
    because a PDF assembled from scans often has no metadata whatsoever.
    """
    if declared := _pdf_metadata_title(source_path):
        return declared
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

    def __init__(
        self,
        *,
        device: str = "cpu",
        max_workers: int | None = None,
        pages_per_task: int = _DEFAULT_PAGES_PER_TASK,
    ) -> None:
        """Configured by the composition root, like everything else.

        It was configured by nothing: `RE_DOCLING_DEVICE` was read straight from
        the environment here while `settings.docling_device` sat declared and
        unread, so the documented setting did nothing at all. Defaults are kept
        on the parameters so the module is still usable zero-arg in tests.
        """
        self._device = device
        self._max_workers = max_workers
        self._pages_per_task = pages_per_task

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

    def _convert(self, source_path: Path) -> tuple[str, str, dict]:
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
        conversion: dict = {}
        if is_pdf and _wants_parallel(total_pages, self._pages_per_task):
            full_text, sections, pages, conversion = _convert_parallel(
                source_path,
                ocr=needs_ocr,
                total_pages=total_pages,
                max_workers=self._max_workers,
                pages_per_task=self._pages_per_task,
            )
        else:
            converter = _get_converter(ocr=needs_ocr, device=self._device)
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
        if conversion:
            # What the conversion actually cost. `peak_worker_mb` is the number
            # `_WORKER_MEMORY_MB` exists to predict, and `halvings` is the signal
            # that it predicted badly — the same role `BackfillReport.halvings`
            # plays for `embedding_batch_size`.
            metadata["conversion"] = conversion

        # Remove empty values
        metadata = {k: v for k, v in metadata.items() if v not in ("", None)}

        return full_text, title, metadata
