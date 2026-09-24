"""Differential benchmark: pure-Python path vs Rust accelerator.

Runs every shipped seam through the same caller production uses, under both
`RE_RUST_BACKEND` settings in one process (the switch reads the env var
dynamically), and re-applies the keep gate so a regression shows up as a
"revert" verdict. Seams that failed the gate were removed from the wheel, not
just from this table: benchmarking them now would time Python against itself. Crossing costs are included — this
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
import zipfile
from dataclasses import dataclass
from pathlib import Path

GATE_RATIO = 1.5
GATE_SAVED_S = 1e-3
BACKENDS = ("python", "rust")
ROOT = Path(__file__).resolve().parents[1]

#: Seams shipped despite missing the gate on this machine's fixtures, and why.
#: A row here prints `KEEP*`; one that starts clearing the gate outright should
#: leave this table, and one that falls further should leave the wheel.
JUDGED_KEEPS = {
    "chunk structural": (
        "conservative ratio sits on the line (1.42-1.50x) with 1.6ms saved per "
        "document; it chunks every structured format the kept parsers produce"
    ),
    "dominant_century letters": (
        "~23x; runs once per document over the whole text, so the 0.9ms saved on "
        "this 95KB fixture grows with the volume"
    ),
}

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
    (tmp / "doc.md").write_text(md_doc, encoding="utf-8")
    (tmp / "doc.html").write_text(html_doc, encoding="utf-8")
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
        f"epub {(tmp / 'doc.epub').stat().st_size // 1024}KB"
    )

    from research_engine.modules.epub import EPUBModule
    from research_engine.modules.html import HTMLModule
    from research_engine.modules.markdown import MarkdownModule
    from research_engine.services.ingestion.chunking.prose_window import ProseWindowChunker
    from research_engine.services.ingestion.chunking.structural import StructuralChunker
    from research_engine.services.text.dates import dominant_century
    from research_engine.services.verification import quote

    os.environ["RE_RUST_BACKEND"] = "python"
    text = fx["mixed"]

    loops: list[asyncio.AbstractEventLoop] = []

    def on_loop(make_coro):
        loop = asyncio.new_event_loop()
        loops.append(loop)
        return lambda: loop.run_until_complete(make_coro())

    md_text, _, md_meta = asyncio.run(MarkdownModule().parse(tmp / "doc.md"))
    sections = md_meta["sections"]
    prose, structural = ProseWindowChunker(), StructuralChunker()

    workloads = [
        Workload("normalize", lambda: quote._normalize(text)),
        Workload("normalize_for_matching", lambda: quote._normalize_for_matching(text)),
        Workload("normalize_with_map", lambda: quote._normalize_with_map(text), trials=7),
        Workload("parse markdown", on_loop(lambda: MarkdownModule().parse(tmp / "doc.md"))),
        Workload("parse html", on_loop(lambda: HTMLModule().parse(tmp / "doc.html"))),
        Workload("parse epub", on_loop(lambda: EPUBModule().parse(tmp / "doc.epub"))),
        Workload("chunk prose", on_loop(lambda: prose.chunk(text, {}))),
        Workload(
            "chunk structural",
            on_loop(lambda: structural.chunk(sections, {}, full_text=md_text)),
        ),
        Workload("dominant_century letters", lambda: dominant_century(fx["letters"])),
    ]

    print(
        f"\n{'workload':26} {'python':>10} {'rust':>10} {'median x':>9} "
        f"{'p25/p75 x':>10} {'saved':>10}  verdict"
    )
    results = []
    for w in workloads:
        r = _measure(w, args.trials or w.trials)
        results.append(r)
        verdict = "KEEP" if r.keep else "KEEP*" if r.name in JUDGED_KEEPS else "revert"
        print(
            f"{r.name:26} {_fmt(r.py_med):>10} {_fmt(r.rs_med):>10} {r.ratio:9.2f} "
            f"{r.conservative:10.2f} {_fmt(r.saved):>10}  {verdict}"
        )

    for loop in loops:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()

    kept = [r.name for r in results if r.keep]
    print(
        f"\nGate: python p25 / rust p75 >= {GATE_RATIO}x and >= {_fmt(GATE_SAVED_S)} saved "
        f"per call (median). Clear: {len(kept)}/{len(results)}."
    )
    for r in results:
        if not r.keep and r.name in JUDGED_KEEPS:
            print(f"KEEP* {r.name}: {JUDGED_KEEPS[r.name]}")
    if failing := [r.name for r in results if not r.keep and r.name not in JUDGED_KEEPS]:
        print(f"REGRESSION: shipped seams now failing the gate: {', '.join(failing)}")


if __name__ == "__main__":
    main()
