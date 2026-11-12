"""Phase 0 measurement: the numbers the gate is stated in.

Reads pages with a named reader, scores every difference against the page's own
truth, and prints the table design §13 asks for: errors per class, marks and
gaps per page, errors per bad page, seconds per page. With ``--audit`` it also
measures an audit reader's sensitivity on the seeded set and the Clopper-Pearson
bound the release rule needs.

What "reader" means here is deliberately narrow. WP0 measures *single readers*:
the pipeline that confirms and marks three readings does not exist yet, so every
difference a reader makes is an unmarked error — which is exactly the quantity
the synthetic page-level bound is about. Table cells, note links and figures are
read as ordinary text in this pass; the specialists that handle them are later
work packages.

The column split is the design's own finding (widest ink-free vertical band in
30-70% of the width, §2), implemented here for measurement only. It never
consults the truth it is being scored against.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageDraw

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_adjudicator as adjudicator  # noqa: E402
from claude_diff_checker import check as claude_check  # noqa: E402
from ocr_synth import metrics  # noqa: E402
from ocr_synth.corpus import Screen, build_corpus  # noqa: E402
from ocr_synth.degrade import Degrade  # noqa: E402
from ocr_synth.seeded_errors import SeededText, build_seeded, can_carry, revert  # noqa: E402
from pipeline_errors import build_pipeline_set  # noqa: E402
from reader_guards import guard_reading  # noqa: E402

Box = tuple[int, int, int, int]
INK = 200
MIN_GUTTER = 20
# A box with less ink than this is not a region: nothing was drawn in it. The
# region inventory decides this properly (WP1); until it exists, the measurement
# must not hand a blank column to a reader, because a VLM asked to read an empty
# region answers anyway — one did, with the integers 1 to 539.
MIN_INK_FRACTION = 0.001
# Text columns are profiled only between these fractions of the height. The
# running head and the page footer are chrome: they are centred, so they put ink
# across the middle of the page and would hide the gutter (design §7.1 masks
# them for the same reason). The measurement must not consult the truth to find
# them, so it uses the outer tenths instead.
BODY_ROWS = (0.12, 0.88)


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def tesseract_text(image: Image.Image) -> str:
    """Read an image with Tesseract psm 6, through stdin/stdout only."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    completed = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng"],
        input=buffer.getvalue(),
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("utf-8", errors="replace").strip()


def easyocr_text(image: Image.Image) -> str:
    """EasyOCR on the region it is given: the candidate third recognizer.

    It brings its own detector and recognizer (CRAFT + a CRNN), which is what
    the design asks of a third reader: an engine whose errors can be *unlike*
    the other two. It reads the region it is handed, so it is called per column.
    """
    import numpy as np

    global _EASYOCR
    if _EASYOCR is None:
        import easyocr

        _EASYOCR = easyocr.Reader(["en"], gpu=False, verbose=False)
    regions = _EASYOCR.readtext(np.array(image.convert("RGB")), detail=0, paragraph=True)
    return "\n".join(regions).strip()


def command_text(command: str) -> Callable[[Image.Image], str]:
    """Any reader that takes a PNG on stdin and prints text on stdout.

    The hook the server-side VLM is measured through: the model runs where the
    GPU is, and this process stays the one that scores it against the truth.
    """

    def read(image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        completed = subprocess.run(
            shlex.split(command), input=buffer.getvalue(), capture_output=True, check=False
        )
        if completed.returncode != 0:
            print(f"reader command failed: {completed.returncode}", file=sys.stderr)
            return ""
        return completed.stdout.decode("utf-8", errors="replace").strip()

    return read


_EASYOCR: object | None = None
READERS: dict[str, Callable[[Image.Image], str]] = {
    "tesseract": tesseract_text,
    "easyocr": easyocr_text,
}


def reader_for(name: str) -> Callable[[Image.Image], str]:
    """A reader by name, a ``command:<cmd>`` hook, or an explicit refusal."""
    if name.startswith("command:"):
        return command_text(name.split(":", 1)[1])
    """A reader by name, or an explicit refusal — never a silent fallback.

    The verified path has no fallback (design §4.1), and neither does the
    measurement: a reader that is not installed makes the run fail rather than
    quietly measure a different one.
    """
    if name not in READERS:
        raise SystemExit(f"reader {name!r} is not available here; installed: {sorted(READERS)}")
    if name == "tesseract" and not tesseract_available():
        raise SystemExit("tesseract is not on PATH")
    return READERS[name]


# --------------------------------------------------------------------------- #
# layout for measurement
# --------------------------------------------------------------------------- #


def ink_columns(grey: Image.Image) -> list[bool]:
    """True where a pixel column has ink, over the page's body rows only."""
    width, height = grey.size
    top, bottom = int(height * BODY_ROWS[0]), int(height * BODY_ROWS[1])
    pixels = grey.load()
    return [any(pixels[x, y] for y in range(top, bottom)) for x in range(width)]


def body_band(height: int) -> Box:
    """The rows a page's text lives in: chrome bands are outside it."""
    return (0, int(height * BODY_ROWS[0]), 1, int(height * BODY_ROWS[1]))


def split_columns(image: Image.Image) -> tuple[Box, ...]:
    """Split a page at the widest ink-free vertical band inside the middle 40%.

    The boxes are clipped to the body band: the running head and the footer are
    chrome that the pipeline masks, so a single-reader baseline that read them
    would be charged for text it is not supposed to see.
    """
    width, height = image.size
    grey = image.convert("L").point(lambda value: 255 if value < INK else 0)
    has_ink = ink_columns(grey)
    top, bottom = body_band(height)[1], body_band(height)[3]
    lower, upper = int(width * 0.30), int(width * 0.70)
    best: tuple[int, int] | None = None
    run_start: int | None = None
    for x in range(lower, upper + 1):
        if x < upper and not has_ink[x]:
            run_start = x if run_start is None else run_start
            continue
        if run_start is not None and (best is None or x - run_start > best[1] - best[0]):
            best = (run_start, x)
        run_start = None
    if best is None or best[1] - best[0] < MIN_GUTTER:
        return ((0, top, width, bottom),)
    return ((0, top, best[0], bottom), (best[1], top, width, bottom))


def error_positions(truth: str, reading: str) -> set[int]:
    """Truth-text offsets a reading gets wrong: the errors a reader actually made."""
    positions: set[int] = set()
    for edit in metrics.classify_errors(metrics.normalize(truth), metrics.normalize(reading)):
        positions.update(range(edit.truth_start, max(edit.truth_end, edit.truth_start + 1)))
    return positions


def ink_fraction(image: Image.Image, box: Box) -> float:
    """The fraction of a box's pixels that are ink."""
    grey = image.convert("L").crop(box).point(lambda value: 255 if value < INK else 0)
    total = max(1, (box[2] - box[0]) * (box[3] - box[1]))
    return sum(grey.histogram()[255:]) / total


def text_regions(image: Image.Image, boxes: Sequence[Box] | None = None) -> tuple[Box, ...]:
    """The boxes with text in them: a blank column is not a region to read."""
    candidates = boxes if boxes is not None else split_columns(image)
    return tuple(box for box in candidates if ink_fraction(image, box) >= MIN_INK_FRACTION)


def unassigned_ink(image: Image.Image, boxes: Sequence[Box]) -> tuple[Box, ...]:
    """Ink outside every assigned region, inside the body band: the gap, measured."""
    grey = image.convert("L").point(lambda value: 255 if value < INK else 0)
    mask = Image.new("L", image.size, 0)
    band = body_band(image.height)
    ImageDraw.Draw(mask).rectangle((band[0], band[1], image.width, band[3]), fill=255)
    ink = ImageChops.multiply(grey, mask)
    covered = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(covered)
    for box in boxes:
        draw.rectangle(box, fill=255)
    bounds = ImageChops.subtract(ink, covered).getbbox()
    return (bounds,) if bounds else ()


def read_screen(screen: Screen, reader: Callable[[Image.Image], str]) -> tuple[str, float, tuple[Box, ...]]:
    """Read every region, guard each reading, and report loops as gaps.

    A guarded region contributes a gap, not text: the design's rule is that ink
    no reader turned into trustworthy text blocks the page (§7.3), and a
    repetition loop is exactly that.
    """
    image = Image.open(io.BytesIO(screen.png)).convert("RGB")
    boxes = text_regions(image)
    started = time.monotonic()
    parts: list[str] = []
    loops: list[Box] = []
    for box in boxes:
        try:
            reading = reader(image.crop(box))
        except subprocess.CalledProcessError as exc:
            print(f"reader failed on {screen.screen_id}: {exc}", file=sys.stderr)
            reading = ""
        guarded = guard_reading(reading, box=box)
        if guarded.gap:
            loops.append(box)
            continue
        parts.append(guarded.text)
    return (
        "\n\n".join(parts),
        time.monotonic() - started,
        unassigned_ink(image, boxes) + tuple(loops),
    )


# --------------------------------------------------------------------------- #
# audit readers
# --------------------------------------------------------------------------- #


# Neither auditor below is an audit reader. `machinery` reports the planted
# spans by construction and `stand-in` character-diffs one reader's output
# against the text it was given, so it reports everything it sees. Both exist to
# exercise the scoring, the seeded set and the Clopper-Pearson bound; neither
# measures any model, and neither is a D1 input.
NEITHER_IS_A_MEASUREMENT = (
    "not a measurement of any audit reader: this number tests the scoring code"
    " and the seeded set, and says nothing about Claude or any local model"
)


def oracle_auditor(screen: Screen, page: SeededText) -> list[metrics.Detection]:
    """Reports exactly the planted spans: the check that the machinery is right."""
    del screen
    return [metrics.Detection(error.final_start, error.final_end) for error in page.errors]


def diff_auditor(readings: dict[str, str]) -> Callable[[Screen, SeededText], list[metrics.Detection]]:
    """A stand-in auditor: compare the page's reading against the text it was given.

    Not the `ClaudeDiffChecker` family and not a model: it re-reads the page with
    the same OCR reader and character-diffs the result against the candidate
    text, so it reports every difference it sees — its sensitivity is near one
    before any model is involved, and its precision is the only thing it could
    ever say anything about.
    """

    def audit(screen: Screen, page: SeededText) -> list[metrics.Detection]:
        reading = readings[screen.screen_id]
        return [
            metrics.Detection(edit.final_start, edit.final_end, snippet=edit.final_text[:20])
            for edit in metrics.classify_errors(reading, page.final)
        ]

    return audit


def sensitivity_report(
    screens: Sequence[Screen],
    name: str,
    auditor: Callable[[Screen, SeededText], list[metrics.Detection]],
    counts: dict[str, int] | None = None,
    seeded: Sequence[SeededText] | None = None,
) -> dict[str, object]:
    """Measure one auditor over a seeded set drawn from this screen set.

    ``seeded`` lets a caller supply the set instead (the pipeline-shaped one built
    from the readers' own mistakes); otherwise the planted set is used.
    """
    if seeded is not None:
        usable = list(screens[: len(seeded)])
    else:
        usable = [screen for screen in screens if can_carry(screen.truth_text, counts)]
    if not usable:
        return {"reader": name, "pages": 0, "planted": 0, "detected": 0, "sensitivity": 0.0, "lower": 0.0}
    if seeded is None:
        seeded = build_seeded(usable, counts)
    lookup = {id(page): screen for screen, page in zip(usable, seeded, strict=True)}
    result = metrics.audit_sensitivity(name, seeded, lambda page: auditor(lookup[id(page)], page))
    return {
        "reader": name,
        "is_measurement": True,
        "pages": result.pages,
        "planted": result.planted,
        "detected": result.detected,
        "sensitivity": round(result.sensitivity, 4),
        "lower": round(result.lower, 4),
        "by_kind": {kind: list(counts) for kind, counts in sorted(result.by_kind.items())},
        "seeded_reverts_to_truth": all(revert(page) == page.truth for page in seeded),
        "skipped_screens": len(screens) - len(usable),
    }


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def coincidence_report(
    rows: list[dict[str, object]], other_name: str, other_readings: dict[str, str]
) -> str:
    """How much of the reader's error set the second reader makes as well.

    A third recognizer earns its place by being wrong in *different* places, so
    the number that matters is the overlap of the two error sets: low overlap
    means the pair sees different pages' worth of trouble.
    """
    lines = [
        "",
        "## Reader coincidence",
        "",
        "| measure | value |",
        "|---|---|",
    ]
    shared = total_first = total_second = union_total = 0
    for row in rows:
        screen_id = str(row["screen"])
        first = row["errors"]  # type: ignore[assignment]
        second = error_positions(str(row["truth"]), other_readings[screen_id])
        shared += len(first & second)  # type: ignore[operator]
        total_first += len(first)  # type: ignore[arg-type]
        total_second += len(second)
        union_total += len(first | second)  # type: ignore[operator]
    jaccard = shared / union_total if union_total else 0.0
    lines += [
        f"| errors of `{row['reader']}` | {total_first} |",
        f"| errors of `{other_name}` | {total_second} |",
        f"| errors both made | {shared} |",
        f"| errors either made | {union_total} |",
        f"| error coincidence (Jaccard) | {jaccard:.4f} |",
    ]
    return "\n".join(lines) + "\n"


def build_report(
    rows: list[dict[str, object]],
    sensitivity: dict[str, object] | None,
    *,
    reader_name: str,
    dpr: int,
    kinds: Iterable[str] | None,
    font: dict[str, object],
    degrade: str | None = None,
) -> str:
    scores = [row["score"] for row in rows]
    totals = metrics.aggregate(scores)
    seconds = [row["seconds"] for row in rows]
    lines = [
        "# Phase 0 measurements",
        "",
        f"- reader `{reader_name}` at {dpr}x, {len(rows)} synthetic screens"
        + (f", kinds {sorted(kinds)}" if kinds else ", all layout classes"),
        f"- font {json.dumps(font, sort_keys=True)}",
        f"- ink {degrade}" if degrade else "- ink plain: no degradation applied",
        "- reader is single: every difference is unmarked, which is what the synthetic bound measures",
        "",
        "## Errors by class",
        "",
        "| class | count |",
        "|---|---|",
    ]
    for kind, count in sorted(totals["by_kind"].items()):  # type: ignore[union-attr]
        lines.append(f"| {kind} | {count} |")
    pages = int(totals["pages"])
    bad = int(totals["pages_with_unmarked"])
    rated = pages or 1  # a report with no rows must still render
    upper = metrics.clopper_pearson_upper(bad, pages) if pages else 0.0
    lines += [
        "",
        "## Gate quantities",
        "",
        "| measure | value | gate |",
        "|---|---|---|",
        f"| pages | {pages} | |",
        f"| unmarked errors | {totals['unmarked_errors']} | |",
        f"| pages with an unmarked error | {bad} | |",
        f"| rate (observed) | {bad / rated:.4f} | |",
        f"| rate (one-sided 95% upper bound) | {upper:.4f} | <= 0.10 |",
        f"| errors per bad page | {totals['errors_per_bad_page']:.2f} | reported |",
        f"| marks | {totals['marks']} | |",
        f"| gap regions | {totals['gaps']} | reported |",
        f"| seconds per page (median) | {statistics.median(seconds) if seconds else 0:.3f} | <= 9 |",
        "",
        "## Per screen",
        "",
        "| screen | kind | columns | seconds | unmarked | gaps | classes |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        score = row["score"]
        lines.append(
            f"| {row['screen']} | {row['kind']} | {row['columns']} | {row['seconds']} | "
            f"{score.unmarked_errors} | {len(score.gaps)} | {json.dumps(score.by_kind, sort_keys=True)} |"
        )
    if sensitivity is not None and "false_alarms" in sensitivity:
        # A clean-page run has nothing to detect, so every reported difference is
        # a false alarm — the number §9.4 cares about, because a false alarm
        # survives the re-read and blocks a book with nothing to fix.
        lines += [
            "",
            "## Audit false alarms (candidate text is the exact truth)",
            "",
            f"- auditor `{sensitivity['reader']}` over {sensitivity['pages']} clean screens",
            f"- differences reported: {sensitivity['false_alarms']} "
            f"({sensitivity['false_alarms_per_screen']} per screen)",
            f"- screens with at least one: {sensitivity['screens_with_a_false_alarm']}",
            f"- API cost: ${sensitivity.get('cost_usd')}",
        ]
    elif sensitivity is not None:
        real = bool(sensitivity.get("is_measurement"))
        lines += ["", "## Audit sensitivity", ""]
        if not real:
            lines += [f"**{NEITHER_IS_A_MEASUREMENT}**", ""]
        if not sensitivity.get("planted"):
            lines.append("No screen could carry the predeclared seeded set; nothing measured.")
        else:
            lower = float(sensitivity["lower"])  # type: ignore[arg-type]
            bound = metrics.release_bound(60, lower)
            lines += [
                f"- auditor `{sensitivity['reader']}`" + ("" if real else f" ({NEITHER_IS_A_MEASUREMENT})"),
                f"- auditor over {sensitivity['pages']} seeded screens",
                f"- planted {sensitivity['planted']}, detected {sensitivity['detected']}, "
                f"sensitivity {sensitivity['sensitivity']}",
                f"- one-sided 95% lower bound `s_L` = {lower} — gate needs `s_L` >= 0.5",
                f"- seeded set reverts to the truth: {sensitivity['seeded_reverts_to_truth']}",
                f"- screens skipped for lack of seeded material: {sensitivity.get('skipped_screens', 0)}",
                f"- release bound at n = 60: {'infinite (no positive lower bound)' if bound == float('inf') else f'{bound:.4f}'}",
                *(
                    [f"- API cost of this measurement: ${sensitivity['cost_usd']}"]
                    if real and sensitivity.get("cost_usd") is not None
                    else []
                ),
                "",
                "| planted class | planted | detected |",
                "|---|---|---|",
            ]
            for kind, counts in sensitivity["by_kind"].items():  # type: ignore[union-attr]
                lines.append(f"| {kind} | {counts[0]} | {counts[1]} |")
    return "\n".join(lines) + "\n"


# The synthetic set is declared here, not tuned against results: six planted
# errors per page so that every layout class can carry the seeds, which the full
# predeclared set cannot (a TOC page has no dictionary words to swap).
SYNTHETIC_COUNTS = {
    "real_word": 1,
    "digit": 1,
    "omission": 1,
    "insertion": 1,
    "punctuation": 1,
    "order": 1,
}


def claude_auditor(images: dict[str, Path], model: str | None):
    """The audit reader D1 asks about: Claude Code in headless mode, diff-only."""
    ledger: list[dict[str, object]] = []
    readings: dict[str, dict[tuple[int, int], str]] = {}

    def audit(screen: Screen, page: SeededText) -> list[metrics.Detection]:
        result = claude_check(images[screen.screen_id], page.final, model=model)
        # The reading belongs to the place, not to the detection record: the
        # adjudication step needs both readings of a disputed place, and the
        # detection only carries where it is.
        readings[screen.screen_id] = {
            (difference.final_start or 0, difference.final_end or 0): difference.reading
            for difference in result.differences
            if difference.located
        }
        ledger.append(
            {
                "screen": screen.screen_id,
                "rejected": result.rejected,
                "differences": len(result.differences),
                "unplaced": sum(1 for difference in result.differences if not difference.located),
                "words_returned": result.raw_word_count,
                "seconds": round(result.seconds, 2),
                "cost_usd": result.cost_usd,
            }
        )
        return [
            metrics.Detection(
                final_start=difference.final_start or 0,
                final_end=difference.final_end or 0,
                snippet=difference.snippet[:20],
            )
            for difference in result.differences
            if difference.located
        ]

    audit.readings = readings  # type: ignore[attr-defined]
    return audit, ledger


def adjudicating(auditor, images: dict[str, Path], model: str | None, rng: random.Random, ledger: list):
    """Wrap an auditor so a difference counts only after an independent look.

    One adjudication call per screen carries every disputed place on it. The
    audit pass's own reading and the pipeline's are given in random order, and an
    "unsure" answer — or no answer at all — leaves the place undetected. The span
    is still marked downstream: marking is unconditional, detection is not.
    """
    readings = getattr(auditor, "readings", {})

    def adjudicated(screen: Screen, page: SeededText) -> list[metrics.Detection]:
        detections = auditor(screen, page)
        if not detections:
            return detections
        by_span = readings.get(screen.screen_id, {})
        places = []
        for detection in detections:
            span = (detection.final_start, detection.final_end)
            places.append((span, page.final[span[0] : span[1]], by_span.get(span, "")))
        disputes = adjudicator.disputes_for(places, rng=rng)
        if not disputes:
            return []
        verdict = adjudicator.adjudicate(images[screen.screen_id], disputes, model=model)
        ledger.append(
            {
                "screen": screen.screen_id,
                "adjudicated": len(disputes),
                "rejected": verdict.rejected,
                "cost_usd": verdict.cost_usd,
                "seconds": round(verdict.seconds, 2),
            }
        )
        confirmed = adjudicator.confirmed_spans(disputes, verdict.verdicts)
        return [detection for detection in detections if (detection.final_start, detection.final_end) in confirmed]

    return adjudicated


def transcriber_auditor(readings: dict[str, str], name: str):
    """An audit reader that transcribes the page and reports where it differs.

    This is the `LocalVlmTranscriber` shape from the design: read the whole
    screen, then diff that reading against the candidate text and report every
    difference. Unlike the stand-in, the reader behind it is the model being
    measured, and the report says so.
    """

    def audit(screen: Screen, page: SeededText) -> list[metrics.Detection]:
        return [
            metrics.Detection(edit.final_start, edit.final_end, snippet=edit.final_text[:20])
            for edit in metrics.classify_errors(readings[screen.screen_id], page.final)
        ]

    audit.reader_name = name  # type: ignore[attr-defined]
    return audit


def _guard_external(readings: dict[str, str], screens: Sequence[Screen]) -> dict[str, str]:
    """Drop looping region answers before they are joined into a screen's text."""
    boxes: dict[str, list[Box]] = {}
    for screen in screens:
        image = Image.open(io.BytesIO(screen.png)).convert("RGB")
        boxes[screen.screen_id] = list(text_regions(image))
    guarded: dict[str, str] = {}
    for key, text in readings.items():
        screen_id, _, suffix = key.partition("__r")
        index = int(suffix) if suffix.isdigit() else None
        box = None
        if index is not None:
            region_boxes = boxes.get(screen_id, [])
            box = region_boxes[index] if index < len(region_boxes) else None
        guarded[key] = guard_reading(text, box=box).text
    return guarded


def _join_regions(readings: dict[str, str]) -> dict[str, str]:
    """Fold ``<screen>__r0``-style region readings into one text per screen."""
    if not any("__r" in key for key in readings):
        return readings
    joined: dict[str, str] = {}
    for key, text in readings.items():
        screen, _, tail = key.partition("__r")
        if not tail:
            joined[key] = text
            continue
        joined[screen] = (joined.get(screen, "") + "\n\n" + text).strip()
    return joined


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reader",
        default="tesseract",
        help="reader to measure: tesseract, easyocr, or command:<cmd> (PNG on stdin, text on stdout)",
    )
    parser.add_argument("--compare", default=None, help="second reader for the coincidence measurement")
    parser.add_argument(
        "--export-regions",
        default=None,
        help="write the region crops a local reader would get, as DIR (for a remote reader)",
    )
    parser.add_argument(
        "--save-readings",
        default=None,
        help="write this run's per-screen readings to PATH (to compare readers on the same text)",
    )
    parser.add_argument(
        "--external",
        default=None,
        help="JSON of {screen_id: text} read elsewhere (e.g. the VLM on the GPU host)",
    )
    parser.add_argument("--kinds", default=None, help="comma-separated layout classes")
    parser.add_argument(
        "--degrade",
        default=None,
        choices=("light", "caps", "tight", "all"),
        help="rink the ink: the degraded-ink variant (small caps, light ink, tight kerning)",
    )
    parser.add_argument("--dpr", type=int, default=1)
    parser.add_argument(
        "--audit",
        default=None,
        choices=("machinery", "stand-in", "claude", "transcriber", "pipeline", "clean"),
        help="machinery/stand-in are scoring-code self-checks; claude measures the real audit reader",
    )
    parser.add_argument("--claude-model", default=None, help="model id for the claude audit reader")
    parser.add_argument(
        "--readings-json",
        default=None,
        help="comma-separated readings files (one per reader) for --audit pipeline",
    )
    parser.add_argument(
        "--pipeline-minimum",
        default=3,
        help="how many readers must agree on a mistake for it to count as pipeline-shaped",
    )
    parser.add_argument("--pages", default=None, help="how many screens the claude audit reads")
    parser.add_argument(
        "--adjudicate",
        action="store_true",
        help="a difference counts as a detection only after a second, independent look (D1(b))",
    )
    parser.add_argument(
        "--adjudicate-seed",
        type=int,
        default=1729,
        help="seed for the random order the two readings are shown in",
    )
    parser.add_argument(
        "--include-derived",
        action="store_true",
        help="include screens whose canonical text is derived (tables, TOC, figures) in the pipeline set",
    )
    parser.add_argument("--json", dest="json_path", default=None)
    return parser.parse_args(argv)


# The ink treatments of the degraded-ink variant. The three readers cannot agree
# on a mistake in crisp synthetic prose — three engines do not fail the same way
# on a clean rendering, so the pipeline-shaped population comes out empty and the
# pipeline's own error rate has no denominator. Real print bends the ink until
# the same wrong reading is the easiest one for every engine.
DEGRADATIONS: dict[str, Degrade] = {
    "light": Degrade(small_caps=False, light=True, kern=0.0, blur=0.3),
    "caps": Degrade(small_caps=True, light=False, kern=0.0, blur=0.0),
    "tight": Degrade(small_caps=False, light=False, kern=-1.0, blur=0.0),
    "all": Degrade(small_caps=True, light=True, kern=-0.8, blur=0.3),
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kinds = tuple(args.kinds.split(",")) if args.kinds else None
    degrade = DEGRADATIONS[args.degrade] if args.degrade else None
    screens = build_corpus(dprs=(args.dpr,), kinds=kinds, degrade=degrade)
    if not screens:
        raise SystemExit(f"no screens for kinds {kinds}")

    # With --external the readings come from a file (the GPU host's), so the
    # local reader is a label here and must not be resolved or refused.
    if args.export_regions:
        target = Path(args.export_regions)
        target.mkdir(parents=True, exist_ok=True)
        written = 0
        for screen in screens:
            image = Image.open(io.BytesIO(screen.png)).convert("RGB")
            for index, box in enumerate(text_regions(image)):
                path = target / f"{screen.screen_id}__r{index}.png"
                image.crop(box).save(path)
                written += 1
        print(f"wrote {written} region crops for {len(screens)} screens to {target}")
        return 0

    # A remote reader answers per region; join its answers in reading order.
    external = json.loads(Path(args.external).read_text()) if args.external else None
    if external is not None:
        # A remote reader's loops are guarded region by region, where the region
        # shape is known, and only what survives is joined into the screen text.
        external = _join_regions(_guard_external(external, screens))
    # An auditor's run names a label, not a local reader: with --external the
    # readings come from a file, and the pipeline and clean audits read nothing
    # locally at all (their readings arrive via --readings-json).
    label_only = external is not None or args.audit in {"pipeline", "clean"}
    reader = (lambda image: "") if label_only else reader_for(args.reader)
    rows: list[dict[str, object]] = []
    readings: dict[str, str] = {}
    errors: dict[str, set[int]] = {}
    for screen in screens:
        if external is not None:
            if screen.screen_id not in external:
                raise SystemExit(f"{args.external} has no reading for {screen.screen_id}")
            image = Image.open(io.BytesIO(screen.png)).convert("RGB")
            reading, seconds, gaps = (
                external[screen.screen_id],
                0.0,
                unassigned_ink(image, split_columns(image)),
            )
        else:
            reading, seconds, gaps = read_screen(screen, reader)
        readings[screen.screen_id] = reading
        errors[screen.screen_id] = error_positions(screen.truth_text, reading)
        image = Image.open(io.BytesIO(screen.png)).convert("RGB")
        rows.append(
            {
                "screen": screen.screen_id,
                "kind": screen.kind,
                "reader": args.reader,
                "truth": screen.truth_text,
                "errors": errors[screen.screen_id],
                "columns": len(text_regions(image)),
                "seconds": round(seconds, 3),
                "score": metrics.score_page(screen.truth_text, reading, marks=(), gaps=gaps, normalized=True),
            }
        )

    sensitivity = None
    ledger: list[dict[str, object]] = []
    if args.audit == "claude":
        import tempfile

        selected = screens[: int(args.pages)] if args.pages else screens
        with tempfile.TemporaryDirectory() as directory:
            images = {}
            for screen in selected:
                path = Path(directory) / f"{screen.screen_id}.png"
                path.write_bytes(screen.png)
                images[screen.screen_id] = path
            auditor, ledger = claude_auditor(images, args.claude_model)
            sensitivity = sensitivity_report(selected, "claude (headless, diff-only)", auditor, counts=SYNTHETIC_COUNTS)
            sensitivity["cost_usd"] = round(sum(float(entry["cost_usd"] or 0) for entry in ledger), 4)
            sensitivity["ledger"] = ledger
    elif args.audit == "machinery":
        sensitivity = sensitivity_report(screens, "machinery (planted spans by construction)", oracle_auditor)
    elif args.audit == "stand-in":
        sensitivity = sensitivity_report(screens, f"stand-in (character diff of {args.reader})", diff_auditor(readings))
    elif args.audit in {"pipeline", "clean"}:
        import tempfile

        extra = {}
        for path in (args.readings_json or "").split(","):
            if path:
                extra[Path(path).stem] = json.loads(Path(path).read_text())
        with tempfile.TemporaryDirectory() as directory:
            images = {}
            for screen in screens:
                image_path = Path(directory) / f"{screen.screen_id}.png"
                image_path.write_bytes(screen.png)
                images[screen.screen_id] = image_path
            auditor, ledger = claude_auditor(images, args.claude_model)
            if args.adjudicate:
                auditor = adjudicating(
                    auditor, images, args.claude_model, random.Random(args.adjudicate_seed), ledger
                )
            if args.audit == "pipeline":
                # The pipeline-shaped set compares readers against the page's
                # canonical text, so it only means something where that text is
                # what a reader sees: a table's canonical form is Markdown and a
                # TOC's carries dot leaders, and every reader "fails" to produce
                # both. Those pages belong to assembly (WP4), not to this set.
                derived = {"table_page", "table_continuation", "toc_page", "figure_page"}
                if not args.include_derived:
                    skipped = [s for s in screens if s.kind in derived]
                    screens = [s for s in screens if s.kind not in derived]
                    if skipped:
                        print(
                            f"skipping {len(skipped)} screen(s) whose canonical text is derived: "
                            f"{sorted({s.kind for s in skipped})}"
                        )
                built = []
                for screen in screens:
                    readings = {
                        name: values[screen.screen_id]
                        for name, values in extra.items()
                        if screen.screen_id in values
                    }
                    if args.reader in readings:
                        readings["vlm"] = readings.pop(args.reader)
                    if len(readings) < 2:
                        continue
                    seeded_page, errors = build_pipeline_set(
                        screen.truth_text, readings, minimum=int(args.pipeline_minimum)
                    )
                    built.append((screen, seeded_page, errors))
                if not built:
                    raise SystemExit(
                        "no screen had readings from two readers on page-visible text; "
                        "pass --readings-json and check --kinds"
                    )
                if all(not page.errors for _, page, _ in built):
                    sensitivity = {
                        "reader": "claude (headless, diff-only) on the readers' agreed errors",
                        "is_measurement": True,
                        "pages": len(built),
                        "planted": 0,
                        "detected": 0,
                        "sensitivity": 0.0,
                        "lower": 0.0,
                        "agreed_errors": 0,
                        "note": (
                            "the readers agreed on no wrong word of the page-visible text, so this "
                            "population is empty and its sensitivity is undefined — not zero"
                        ),
                        "readers": sorted(extra),
                        "minimum_agreement": int(args.pipeline_minimum),
                    }
                    print(
                        "the readers agreed on no wrong word of the page-visible text: the "
                        "pipeline-shaped population is empty on this screen set"
                    )
                    sensitivity["cost_usd"] = 0.0
                    sensitivity["ledger"] = []
                    if args.json_path:
                        Path(args.json_path).write_text(json.dumps({"sensitivity": sensitivity}, indent=2))
                    return 0
                selected = [screen for screen, _, _ in built]
                seeded_pages = [page for _, page, _ in built]
                sensitivity = sensitivity_report(
                    selected,
                    "claude (headless, diff-only) on the readers' agreed errors",
                    auditor,
                    seeded=seeded_pages,
                )
                sensitivity["agreed_errors"] = sum(len(errors) for _, _, errors in built)
                sensitivity["agreed_by_kind"] = {
                    kind: sum(1 for _, _, errors in built for error in errors if error.kind == kind)
                    for kind in {error.kind for _, _, errors in built for error in errors}
                }
                sensitivity["readers"] = sorted(extra)
                sensitivity["minimum_agreement"] = int(args.pipeline_minimum)
            else:
                # Every difference reported against the exact truth is a false
                # alarm: there is no error on the page to have found.
                reported = 0
                for screen in screens:
                    result = claude_check(images[screen.screen_id], screen.truth_text, model=args.claude_model)
                    reported += len(result.differences)
                    ledger.append(
                        {
                            "screen": screen.screen_id,
                            "differences": len(result.differences),
                            "unplaced": sum(1 for d in result.differences if not d.located),
                            "words_returned": result.raw_word_count,
                            "seconds": round(result.seconds, 2),
                            "cost_usd": result.cost_usd,
                            "rejected": result.rejected,
                        }
                    )
                sensitivity = {
                    "reader": "claude (headless, diff-only) on clean pages",
                    "is_measurement": True,
                    "pages": len(screens),
                    "false_alarms": reported,
                    "false_alarms_per_screen": round(reported / len(screens), 3),
                    "screens_with_a_false_alarm": sum(1 for entry in ledger if entry["differences"]),
                    "ledger": ledger,
                }
            sensitivity["cost_usd"] = round(sum(float(entry["cost_usd"] or 0) for entry in ledger), 4)
            sensitivity["ledger"] = ledger
    elif args.audit == "transcriber":
        label = f"{args.reader} as transcriber (diff against the candidate text)"
        sensitivity = sensitivity_report(screens, label, transcriber_auditor(readings, label), counts=SYNTHETIC_COUNTS)

    if args.save_readings:
        Path(args.save_readings).write_text(json.dumps(readings, indent=2), encoding="utf-8")
    document = build_report(
        rows,
        sensitivity,
        reader_name=args.reader,
        dpr=args.dpr,
        kinds=kinds,
        font=screens[0].font,
        degrade=degrade.label if degrade else None,
    )
    if args.compare:
        other = reader_for(args.compare)
        other_readings = {}
        for screen in screens:
            other_readings[screen.screen_id], _, _ = read_screen(screen, other)
        document += coincidence_report(rows, args.compare, other_readings)
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(
                {
                    "reader": args.reader,
                    "dpr": args.dpr,
                    "rows": [
                        {
                            "screen": row["screen"],
                            "kind": row["kind"],
                            "seconds": row["seconds"],
                            "unmarked": row["score"].unmarked_errors,
                            "gaps": len(row["score"].gaps),
                            "by_kind": row["score"].by_kind,
                        }
                        for row in rows
                    ],
                    "sensitivity": sensitivity,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    print(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
