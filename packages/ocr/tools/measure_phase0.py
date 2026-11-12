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

from claude_diff_checker import check as claude_check  # noqa: E402
from ocr_synth import metrics  # noqa: E402
from ocr_synth.corpus import Screen, build_corpus  # noqa: E402
from ocr_synth.seeded_errors import SeededText, build_seeded, can_carry, revert  # noqa: E402

Box = tuple[int, int, int, int]
INK = 200
MIN_GUTTER = 20
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
    image = Image.open(io.BytesIO(screen.png)).convert("RGB")
    boxes = split_columns(image)
    started = time.monotonic()
    parts: list[str] = []
    for box in boxes:
        try:
            parts.append(reader(image.crop(box)))
        except subprocess.CalledProcessError as exc:
            print(f"reader failed on {screen.screen_id}: {exc}", file=sys.stderr)
            parts.append("")
    return "\n\n".join(part for part in parts if part), time.monotonic() - started, unassigned_ink(image, boxes)


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
) -> dict[str, object]:
    """Measure one auditor over the seeded set drawn from this screen set."""
    usable = [screen for screen in screens if can_carry(screen.truth_text, counts)]
    if not usable:
        return {"reader": name, "pages": 0, "planted": 0, "detected": 0, "sensitivity": 0.0, "lower": 0.0}
    seeded = build_seeded(usable, counts)
    lookup = {id(page): screen for screen, page in zip(usable, seeded, strict=True)}
    result = metrics.audit_sensitivity(name, seeded, lambda page: auditor(lookup[id(page)], page))
    return {
        "reader": name,
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
        f"| rate (observed) | {bad / pages:.4f} | |",
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
    if sensitivity is not None:
        real = sensitivity["reader"].startswith(("claude", "Qwen", "vlm"))
        lines += ["", "## Audit sensitivity", ""]
        if not real:
            lines += [f"**{NEITHER_IS_A_MEASUREMENT}**", ""]
        if not sensitivity.get("planted"):
            lines.append("No screen could carry the predeclared seeded set; nothing measured.")
        else:
            lower = float(sensitivity["lower"])  # type: ignore[arg-type]
            bound = metrics.release_bound(60, lower)
            lines += [
                f"- auditor `{sensitivity['reader']}` ({NEITHER_IS_A_MEASUREMENT})",
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

    def audit(screen: Screen, page: SeededText) -> list[metrics.Detection]:
        result = claude_check(images[screen.screen_id], page.final, model=model)
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

    return audit, ledger


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reader",
        default="tesseract",
        help="reader to measure: tesseract, easyocr, or command:<cmd> (PNG on stdin, text on stdout)",
    )
    parser.add_argument("--compare", default=None, help="second reader for the coincidence measurement")
    parser.add_argument(
        "--external",
        default=None,
        help="JSON of {screen_id: text} read elsewhere (e.g. the VLM on the GPU host)",
    )
    parser.add_argument("--kinds", default=None, help="comma-separated layout classes")
    parser.add_argument("--dpr", type=int, default=1)
    parser.add_argument(
        "--audit",
        default=None,
        choices=("machinery", "stand-in", "claude", "transcriber"),
        help="machinery/stand-in are scoring-code self-checks; claude measures the real audit reader",
    )
    parser.add_argument("--claude-model", default=None, help="model id for the claude audit reader")
    parser.add_argument("--pages", default=None, help="how many screens the claude audit reads")
    parser.add_argument("--json", dest="json_path", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kinds = tuple(args.kinds.split(",")) if args.kinds else None
    reader = reader_for(args.reader)
    screens = build_corpus(dprs=(args.dpr,), kinds=kinds)
    if not screens:
        raise SystemExit(f"no screens for kinds {kinds}")

    external = json.loads(Path(args.external).read_text()) if args.external else None
    rows: list[dict[str, object]] = []
    readings: dict[str, str] = {}
    errors: dict[str, set[int]] = {}
    for screen in screens:
        if external is not None:
            if screen.screen_id not in external:
                raise SystemExit(f"{args.external} has no reading for {screen.screen_id}")
            image = Image.open(io.BytesIO(screen.png)).convert("RGB")
            reading, seconds, gaps = external[screen.screen_id], 0.0, unassigned_ink(image, split_columns(image))
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
                "columns": len(split_columns(image)),
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
    elif args.audit == "transcriber":
        label = f"{args.reader} as transcriber (diff against the candidate text)"
        sensitivity = sensitivity_report(screens, label, transcriber_auditor(readings, label), counts=SYNTHETIC_COUNTS)

    document = build_report(rows, sensitivity, reader_name=args.reader, dpr=args.dpr, kinds=kinds, font=screens[0].font)
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
