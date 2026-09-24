"""Differential benchmark: pure-Python path vs Rust accelerator.

Runs each workload through the same seam callers production uses, under both
`RE_RUST_BACKEND` settings in one process (the switch reads the env var
dynamically), and applies the keep gate. Crossing costs are included — this
measures what users feel, not crate microbenchmarks.

Method, and the confound each choice removes:

- Pinned to one CPU (`--cpu`), executor threads included. Unpinned on a
  hybrid P/E-core laptop, best-of-5 html swung 0.90x-2.10x between repeats.
- Backends alternate trial by trial, so drift (thermal, frequency, GC
  pressure) lands on both sides instead of whichever ran second.
- Medians and quartiles, not best-of-N. The gate reads the conservative
  ratio: Python's 25th percentile over Rust's 75th.
- One event loop per workload, so async rows pay a warm executor like the
  server does, not ~0.5ms of thread-pool setup per call.
- Sub-millisecond calls run `number` times per sample and report per call.
- Fixtures are real prose (`corpus-engine-docs/docs`) plus pointed Hebrew;
  sizes are printed, not asserted in labels.

Keep gate: conservative ratio >= 1.5x AND >= 1ms saved per call (a call is
one user-facing operation: a document parsed or chunked, a query built).
A seam that clears the ratio but saves microseconds is maintenance cost,
not speed.

Release wheel only: plain `maturin build` is the dev profile (opt-level 0,
~10x slow against identical sources), so the script refuses any
`marginalia_rs.BUILD_PROFILE` but "release". Build with
`maturin build --release` (release.yml does; ci.yml's parity wheel is dev).

Usage: `uv run python scripts/bench_accelerator.py [--trials N] [--cpu N]`
"""

import argparse
import asyncio
import gc
import html
import io
import os
import random
import statistics
import sys
import tempfile
import textwrap
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

GATE_RATIO = 1.5
GATE_SAVED_S = 1e-3
BACKENDS = ("python", "rust")
ROOT = Path(__file__).resolve().parents[1]

#: Genesis 1:1-3 (WLC), pointed and accented: the combining-mark density
#: that NFKC and the script-aware token estimates actually see in this corpus.
HEBREW = (
    "בְּרֵאשִׁ֖ית בָּרָ֣א אֱלֹהִ֑ים אֵ֥ת הַשָּׁמַ֖יִם וְאֵ֥ת הָאָֽרֶץ׃ "
    "וְהָאָ֗רֶץ הָיְתָ֥ה תֹ֙הוּ֙ וָבֹ֔הוּ וְחֹ֖שֶׁךְ עַל־פְּנֵ֣י תְה֑וֹם "
    "וְר֣וּחַ אֱלֹהִ֔ים מְרַחֶ֖פֶת עַל־פְּנֵ֥י הַמָּֽיִם׃ "
    "וַיֹּ֥אמֶר אֱלֹהִ֖ים יְהִ֣י א֑וֹר וַֽיְהִי־אֽוֹר׃"
)


@dataclass
class Workload:
    name: str
    fn: object
    #: Calls per timed sample; the sample is divided back to one call.
    number: int = 1
    trials: int = 15


@dataclass
class Result:
    name: str
    py_med: float
    rs_med: float
    ratio: float
    conservative: float
    saved: float

    @property
    def keep(self) -> bool:
        return self.conservative >= GATE_RATIO and self.saved >= GATE_SAVED_S


def _sample(fn, number: int) -> float:
    start = time.perf_counter()
    for _ in range(number):
        fn()
    return (time.perf_counter() - start) / number


def _measure(w: Workload, trials: int, warmup: int = 3) -> Result:
    for backend in BACKENDS:
        os.environ["RE_RUST_BACKEND"] = backend
        for _ in range(warmup):
            w.fn()
    gc.collect()
    samples: dict[str, list[float]] = {b: [] for b in BACKENDS}
    for i in range(trials):
        for backend in BACKENDS if i % 2 == 0 else BACKENDS[::-1]:
            os.environ["RE_RUST_BACKEND"] = backend
            samples[backend].append(_sample(w.fn, w.number))
    py, rs = samples["python"], samples["rust"]
    py_q, rs_q = statistics.quantiles(py, n=4), statistics.quantiles(rs, n=4)
    py_med, rs_med = statistics.median(py), statistics.median(rs)
    return Result(w.name, py_med, rs_med, py_med / rs_med, py_q[0] / rs_q[2], py_med - rs_med)


def _fmt(seconds: float) -> str:
    magnitude = abs(seconds)
    if magnitude >= 1:
        return f"{seconds:.2f}s"
    if magnitude >= 1e-3:
        return f"{seconds * 1e3:.2f}ms"
    return f"{seconds * 1e6:.2f}us"


def _pin(cpu: int) -> str:
    if cpu < 0:
        return "unpinned (results are noisy on hybrid CPUs)"
    if not hasattr(os, "sched_setaffinity"):
        return "unpinned (no sched_setaffinity on this platform)"
    allowed = sorted(os.sched_getaffinity(0))
    chosen = cpu if cpu in allowed else allowed[0]
    os.sched_setaffinity(0, {chosen})
    governor = Path(f"/sys/devices/system/cpu/cpu{chosen}/cpufreq/scaling_governor")
    try:
        return f"pinned to cpu {chosen} (governor: {governor.read_text().strip()})"
    except OSError:
        return f"pinned to cpu {chosen}"


def _epub(parts: list[list[str]]) -> bytes:
    """A stdlib-only EPUB: one XHTML chapter per part, in spine order."""
    dc = 'xmlns:dc="http://purl.org/dc/elements/1.1/"'
    files = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": (
            '<?xml version="1.0"?><container '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile media-type="application/oebps-package+xml" '
            'full-path="EPUB/content.opf"/></rootfiles></container>'
        ),
    }
    manifest, spine = [], []
    for i, part in enumerate(parts):
        manifest.append(f'<item href="c{i}.xhtml" id="c{i}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
        paras = "".join(f"<p>{html.escape(p)}</p>" for p in part)
        files[f"EPUB/c{i}.xhtml"] = f"<html><body><h1>Chapter {i}</h1>{paras}</body></html>"
    files["EPUB/content.opf"] = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"><metadata>'
        f"<dc:title {dc}>Corpus docs</dc:title><dc:language {dc}>en</dc:language>"
        f"</metadata><manifest>{''.join(manifest)}</manifest><spine>{''.join(spine)}</spine>"
        "</package>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buf.getvalue()


def _fixtures(tmp: Path) -> dict:
    """Real prose in the shapes each workload consumes."""
    sources = sorted((ROOT / "corpus-engine-docs" / "docs").glob("*.md"))
    md_doc = "\n\n".join(p.read_text(encoding="utf-8") for p in sources)
    paragraphs = [p.strip() for p in md_doc.split("\n\n") if p.strip()]

    # Scanned-book shape for normalization: hard-wrapped columns, pointed
    # Hebrew every few paragraphs.
    mixed = "\n\n".join(
        (HEBREW if i % 5 == 0 else "") + "\n" + textwrap.fill(p, 72)
        for i, p in enumerate(paragraphs)
    )

    body = []
    for p in paragraphs:
        if p.startswith("#"):
            body.append(f"</section><section><h2>{html.escape(p.lstrip('# '))}</h2>")
        else:
            body.append(f"<p>{html.escape(p)}</p>")
    html_doc = (
        "<!doctype html><html><head><title>Corpus docs</title></head><body><article>"
        f"<section>{''.join(body)}</section></article></body></html>"
    )

    rng = random.Random(20260923)
    months = ["January", "March", "June", "September", "December"]
    letters = "\n\n".join(
        f"Camp near Falmouth, Va., {rng.choice(months)} {rng.randint(1, 28)}th 18{rng.randint(61, 65)}"
        f"\n\nDear Mother, {p}"
        for p in paragraphs[:400]
    )

    prose = [p for p in paragraphs if not p.startswith("#")]
    parts = [prose[i : i + 8] for i in range(0, 320, 8)]
    (tmp / "doc.epub").write_bytes(_epub(parts))
    # The Rust TEI parser rejects `&amp;` (xml.rs `resolve_entities` decodes
    # it, then loops on the bare `&`), so this fixture carries no ampersands.
    divs = "".join(
        f"<div><head>Part {i}</head>"
        + "".join(f"<p>{html.escape(p.replace('&', 'and'))}</p>" for p in part)
        + "</div>"
        for i, part in enumerate(parts)
    )
    (tmp / "doc.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><TEI xmlns="http://www.tei-c.org/ns/1.0">'
        "<teiHeader><titleStmt><title>Corpus docs</title></titleStmt></teiHeader>"
        f"<text><body>{divs}</body></text></TEI>",
        encoding="utf-8",
    )
    (tmp / "doc.md").write_text(md_doc, encoding="utf-8")
    (tmp / "doc.html").write_text(html_doc, encoding="utf-8")
    (tmp / "doc.txt").write_text(mixed, encoding="utf-8")
    return {"md": md_doc, "html": html_doc, "mixed": mixed, "letters": letters}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, help="override every workload's trial count (>=4)")
    parser.add_argument("--cpu", type=int, default=2, help="CPU to pin to; -1 disables pinning")
    args = parser.parse_args()
    if args.trials is not None and args.trials < 4:
        parser.error("--trials must be >= 4 (quartiles need samples)")

    try:
        import marginalia_rs
    except ImportError:
        print("marginalia_rs not installed; nothing to compare against.")
        sys.exit(2)
    profile = getattr(marginalia_rs, "BUILD_PROFILE", "unknown (pre-BUILD_PROFILE wheel)")
    if profile != "release":
        print(f"marginalia_rs build profile is {profile}; benchmark a release wheel only:")
        print("  maturin build --release --manifest-path crates/marginalia-py/Cargo.toml")
        sys.exit(2)

    print(_pin(args.cpu))
    tmp = Path(tempfile.mkdtemp(prefix="bench_accel_"))
    fx = _fixtures(tmp)
    print(
        f"fixtures: markdown {len(fx['md'].encode()) // 1024}KB, "
        f"html {len(fx['html'].encode()) // 1024}KB, "
        f"mixed text {len(fx['mixed'].encode()) // 1024}KB, "
        f"letters {len(fx['letters'].encode()) // 1024}KB, "
        f"epub {(tmp / 'doc.epub').stat().st_size // 1024}KB, "
        f"tei {(tmp / 'doc.xml').stat().st_size // 1024}KB"
    )

    from research_engine.adapters.storage.postgres.repositories.passages import _is_known_config
    from research_engine.domain.nodes import DocumentNode
    from research_engine.modules.epub import EPUBModule
    from research_engine.modules.html import HTMLModule
    from research_engine.modules.markdown import MarkdownModule
    from research_engine.modules.plain_text import PlainTextModule
    from research_engine.modules.tei_xml import TEIXMLModule
    from research_engine.services.ingestion.chunking.fixed_window import FixedWindowChunker
    from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker
    from research_engine.services.ingestion.chunking.structural import StructuralChunker
    from research_engine.services.ingestion.chunking.whole_or_paragraph import (
        WholeOrParagraphChunker,
    )
    from research_engine.services.search import hybrid
    from research_engine.services.search.windows import _build_window, choose_window
    from research_engine.services.text.anchoring import Span
    from research_engine.services.text.dates import (
        dominant_century,
        parse_fuzzy_date,
        scan_dates,
    )
    from research_engine.services.verification import quote
    from research_engine.services.works import drafting

    os.environ["RE_RUST_BACKEND"] = "python"
    text = fx["mixed"]

    # Production shapes: search fuses k_vec=k_kw=100 hits, then windows each
    # hit inside a book-sized canonical text.
    rng = random.Random(20260923)
    hit_ids = [uuid.uuid4() for _ in range(150)]
    vec_hits = [(pid, rng.random()) for pid in hit_ids[:100]]
    kw_hits = [(pid, rng.random()) for pid in hit_ids[50:]]
    doc_id = uuid.uuid4()
    chain = [
        DocumentNode(
            id=uuid.uuid4(),
            document_id=doc_id,
            parent_id=None,
            path="r" + ".n0" * depth,
            depth=depth,
            position=0,
            node_type="section",
            title=title,
            char_start=start,
            char_end=end,
            created_at=datetime.now(UTC),
        )
        for start, end, depth, title in [
            (0, 900_000, 0, "A Marginal Jew"),
            (100_000, 124_267, 1, "Chapter 14"),
        ]
    ]
    span = Span(110_000, 112_000)
    plan = choose_window(span, chain, budget_chars=6_000, min_chars=800)
    hit = SimpleNamespace(
        id=uuid.uuid4(),
        document_id=doc_id,
        char_start=span.start,
        char_end=span.end,
        node_id=chain[-1].id,
        text="chunk",
    )
    book = (text * 3)[:900_000]
    cite = uuid.uuid4()
    drafted = "\n\n".join(
        p + (f" {drafting._format_marker(cite)}" if i % 3 == 0 else "")
        for i, p in enumerate(fx["md"].split("\n\n"))
    )

    loops: list[asyncio.AbstractEventLoop] = []

    def on_loop(make_coro):
        loop = asyncio.new_event_loop()
        loops.append(loop)
        return lambda: loop.run_until_complete(make_coro())

    md_text, _, md_meta = asyncio.run(MarkdownModule().parse(tmp / "doc.md"))
    sections = md_meta["sections"]
    prose, fixed = ProseWindowChunker(), FixedWindowChunker()
    structural, whole = StructuralChunker(), WholeOrParagraphChunker()

    workloads = [
        Workload("normalize", lambda: quote._normalize(text)),
        Workload("normalize_for_matching", lambda: quote._normalize_for_matching(text)),
        Workload("normalize_with_map", lambda: quote._normalize_with_map(text), trials=7),
        Workload("parse markdown", on_loop(lambda: MarkdownModule().parse(tmp / "doc.md"))),
        Workload("parse html", on_loop(lambda: HTMLModule().parse(tmp / "doc.html"))),
        Workload("parse plain", on_loop(lambda: PlainTextModule().parse(tmp / "doc.txt"))),
        Workload("parse epub", on_loop(lambda: EPUBModule().parse(tmp / "doc.epub"))),
        Workload("parse tei", on_loop(lambda: TEIXMLModule().parse(tmp / "doc.xml"))),
        Workload("chunk prose", on_loop(lambda: prose.chunk(text, {}))),
        Workload("chunk fixed", on_loop(lambda: fixed.chunk(text, {}))),
        Workload("chunk whole_or_paragraph", on_loop(lambda: whole.chunk(text, {}))),
        Workload(
            "chunk structural",
            on_loop(lambda: structural.chunk(sections, {}, full_text=md_text)),
        ),
        Workload("scan_dates letters", lambda: scan_dates(fx["letters"], century=1800), trials=7),
        Workload("dominant_century letters", lambda: dominant_century(fx["letters"])),
        Workload("parse_fuzzy_date", lambda: parse_fuzzy_date("March 24th, 1862"), 5000),
        Workload("find_markers drafted", lambda: drafting._find_markers(drafted), 20),
        Workload("rrf_fuse 2x100", lambda: hybrid._rrf_fuse(vec_hits, kw_hits), number=200),
        Workload("weighted_fuse 2x100", lambda: hybrid._weighted_fuse(vec_hits, kw_hits), 200),
        Workload(
            "choose_window",
            lambda: choose_window(span, chain, budget_chars=6_000, min_chars=800),
            2000,
        ),
        Workload("build_window 900KB doc", lambda: _build_window(hit, plan, chain, book), 200),
        Workload("is_known_config", lambda: _is_known_config("english"), 5000),
    ]

    print(
        f"\n{'workload':26} {'python':>10} {'rust':>10} {'median x':>9} "
        f"{'p25/p75 x':>10} {'saved':>10}  verdict"
    )
    results = []
    for w in workloads:
        r = _measure(w, args.trials or w.trials)
        results.append(r)
        print(
            f"{r.name:26} {_fmt(r.py_med):>10} {_fmt(r.rs_med):>10} {r.ratio:9.2f} "
            f"{r.conservative:10.2f} {_fmt(r.saved):>10}  {'KEEP' if r.keep else 'revert'}"
        )

    for loop in loops:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()

    kept = [r.name for r in results if r.keep]
    print(
        f"\nGate: python p25 / rust p75 >= {GATE_RATIO}x and >= {_fmt(GATE_SAVED_S)} saved "
        f"per call (median). Keep {len(kept)}/{len(results)}: {', '.join(kept) or 'none'}"
    )


if __name__ == "__main__":
    main()
