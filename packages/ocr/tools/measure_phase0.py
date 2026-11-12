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


READERS: dict[str, Callable[[Image.Image], str]] = {"tesseract": tesseract_text}


def reader_for(name: str) -> Callable[[Image.Image], str]:
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


def oracle_auditor(screen: Screen, page: SeededText) -> list[metrics.Detection]:
    """Reports exactly the planted spans: the check that the machinery is right."""
    del screen
    return [metrics.Detection(error.final_start, error.final_end) for error in page.errors]


def diff_auditor(readings: dict[str, str]) -> Callable[[Screen, SeededText], list[metrics.Detection]]:
    """A diff-only auditor: compare the page's reading against the text it was given.

    This is the honest stand-in for the `ClaudeDiffChecker` family until WP5
    builds the real one: the same shape of judgement, a different reader behind
    it. It is what makes the family's *precision* cost measurable, which is the
    number D1 turns on.
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
) -> dict[str, object]:
    """Measure one auditor over the seeded set drawn from this screen set."""
    usable = [screen for screen in screens if can_carry(screen.truth_text)]
    if not usable:
        return {"reader": name, "pages": 0, "planted": 0, "detected": 0, "sensitivity": 0.0, "lower": 0.0}
    seeded = build_seeded(usable)
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
        lines += ["", "## Audit sensitivity", ""]
        if not sensitivity.get("planted"):
            lines.append("No screen could carry the predeclared seeded set; nothing measured.")
        else:
            lower = float(sensitivity["lower"])  # type: ignore[arg-type]
            bound = metrics.release_bound(60, lower)
            lines += [
                f"- auditor `{sensitivity['reader']}` on {sensitivity['pages']} seeded screens",
                f"- planted {sensitivity['planted']}, detected {sensitivity['detected']}, "
                f"sensitivity {sensitivity['sensitivity']}",
                f"- one-sided 95% lower bound `s_L` = {lower} — gate needs `s_L` >= 0.5",
                f"- seeded set reverts to the truth: {sensitivity['seeded_reverts_to_truth']}",
                f"- screens skipped for lack of seeded material: {sensitivity.get('skipped_screens', 0)}",
                f"- release bound at n = 60: {'infinite (no positive lower bound)' if bound == float('inf') else f'{bound:.4f}'}",
                "",
                "| planted class | planted | detected |",
                "|---|---|---|",
            ]
            for kind, counts in sensitivity["by_kind"].items():  # type: ignore[union-attr]
                lines.append(f"| {kind} | {counts[0]} | {counts[1]} |")
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reader", default="tesseract", help="reader to measure (tesseract)")
    parser.add_argument("--kinds", default=None, help="comma-separated layout classes")
    parser.add_argument("--dpr", type=int, default=1)
    parser.add_argument("--audit", default=None, choices=("oracle", "diff"), help="audit reader to measure")
    parser.add_argument("--json", dest="json_path", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    kinds = tuple(args.kinds.split(",")) if args.kinds else None
    reader = reader_for(args.reader)
    screens = build_corpus(dprs=(args.dpr,), kinds=kinds)
    if not screens:
        raise SystemExit(f"no screens for kinds {kinds}")

    rows: list[dict[str, object]] = []
    readings: dict[str, str] = {}
    for screen in screens:
        reading, seconds, gaps = read_screen(screen, reader)
        readings[screen.screen_id] = reading
        image = Image.open(io.BytesIO(screen.png)).convert("RGB")
        rows.append(
            {
                "screen": screen.screen_id,
                "kind": screen.kind,
                "columns": len(split_columns(image)),
                "seconds": round(seconds, 3),
                "score": metrics.score_page(screen.truth_text, reading, marks=(), gaps=gaps, normalized=True),
            }
        )

    sensitivity = None
    if args.audit == "oracle":
        sensitivity = sensitivity_report(screens, "oracle", oracle_auditor)
    elif args.audit == "diff":
        sensitivity = sensitivity_report(screens, f"diff:{args.reader}", diff_auditor(readings))

    document = build_report(rows, sensitivity, reader_name=args.reader, dpr=args.dpr, kinds=kinds, font=screens[0].font)
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
