"""Why the agreed omissions are omissions: merged words, dropped lines, or clipping.

A three-reader confirm step reports a word as an omitted error when none of the
readers produced it. That can happen for three very different reasons, and only
one of them is an unmarked error in the design's sense:

* **merged** — tight kerning joined two words into one (`rn` + neighbour read as
  `m…`), so the second word is "missing" while its ink was read;
* **line missing** — a whole line produced no text at all: design §7.3 calls that
  a *gap*, which marks the page and is never an unmarked error;
* **outside the body band** — the line sits outside the measurement's clipped band
  (BODY_ROWS), so it is a measurement artifact, not a book error.

This tool classifies every agreed omission and reports where on the page each one
sits, so a population built from omissions can be read for what it is.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from difflib import SequenceMatcher  # noqa: E402

from ocr_synth.corpus import Screen, build_corpus  # noqa: E402
from ocr_synth.seeded_errors import words_of  # noqa: E402
from pipeline_errors import agreed_errors  # noqa: E402

BODY_ROWS = (0.12, 0.88)


def line_boxes(screen: Screen) -> list[tuple[float, float, float, float]]:
    return [line.box for region in screen.regions for line in region.lines]


def run_words(screen: Screen) -> list[tuple[str, int]]:
    """Every run's word, in reading order, with the line it sits on."""
    sequence: list[tuple[str, int]] = []
    line_index = 0
    for region in screen.regions:
        for line in region.lines:
            for run in line.runs:
                for word in words_of(run.text):
                    sequence.append((word.text, line_index))
            line_index += 1
    return sequence


def word_lines(screen: Screen) -> tuple[list[int | None], int]:
    """For each truth word, the line it sits on, aligned against the runs.

    The truth is *assembled* from the runs (with separators and de-hyphenation),
    so the two sequences agree on most words and diverge at the joins. A
    SequenceMatcher over them maps every word it can and leaves the rest None,
    which is reported as a fraction rather than silently guessed.
    """
    runs = run_words(screen)
    truth = [word.text for word in words_of(screen.truth_text)]
    lines: list[int | None] = [None] * len(truth)
    matcher = SequenceMatcher(None, truth, [word for word, _line in runs], autojunk=False)
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(i2 - i1):
            lines[i1 + offset] = runs[j1 + offset][1]
    mapped = sum(1 for line in lines if line is not None)
    return lines, mapped


def classify(screen: Screen, readings: dict[str, str], *, minimum: int) -> dict[str, object]:
    truth_words = words_of(screen.truth_text)
    lines, mapped = word_lines(screen)
    boxes = line_boxes(screen)
    errors = agreed_errors(screen.truth_text, readings, minimum=minimum)
    omissions = [error for error in errors if error.kind == "agreed_omission"]
    indexes = [error.word_index for error in omissions]

    # A run of consecutive omitted words is a line that produced nothing: design
    # §7.3's gap, not an unmarked error.
    runs: list[list[int]] = []
    for index in indexes:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    in_long_run = {index for run in runs if len(run) >= 5 for index in run}

    merged = line_missing = outside = other = 0
    heights: Counter[int] = Counter()
    for error in omissions:
        index = error.word_index
        absorbed = any(_absorbed(run_word, reading) for reading in readings.values() for run_word in [error.truth_text])
        if absorbed:
            merged += 1
            continue
        if index in in_long_run:
            line_missing += 1
            continue
        other += 1
        line = lines[index] if index < len(lines) else None
        if line is not None and line < len(boxes):
            box = boxes[line]
            heights[int(100 * ((box[1] + box[3]) / 2) / screen.height)] += 1
            if (box[1] / screen.height) < BODY_ROWS[0] or (box[3] / screen.height) > BODY_ROWS[1]:
                outside += 1

    return {
        "screen": screen.screen_id,
        "kind": screen.kind,
        "truth_words": len(truth_words),
        "line_mapping_fraction": round(mapped / max(1, len(truth_words)), 3),
        "omissions": len(omissions),
        "merged": merged,
        "line_missing": line_missing,
        "other": other,
        "outside_body_band": outside,
        "line_runs": sorted((len(run) for run in runs), reverse=True)[:8],
        "height_histogram": dict(sorted(heights.items())),
    }


def _absorbed(word: str, reading: str) -> bool:
    """Whether this word was read as part of a longer word (a kern join)."""
    folded = word.casefold()
    if len(folded) < 2:
        return False
    return any(
        len("".join(c for c in candidate.casefold() if c.isalnum())) > len(folded)
        and folded in "".join(c for c in candidate.casefold() if c.isalnum())
        for candidate in reading.split()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readings-json", required=True, help="comma-separated readings files")
    parser.add_argument("--kinds", default=None)
    parser.add_argument("--degrade", default=None, choices=("light", "caps", "tight", "all"))
    parser.add_argument("--dpr", type=int, default=3)
    parser.add_argument("--pipeline-minimum", type=int, default=3)
    args = parser.parse_args(argv)

    from measure_phase0 import DEGRADATIONS

    kinds = tuple(args.kinds.split(",")) if args.kinds else None
    degrade = DEGRADATIONS[args.degrade] if args.degrade else None
    screens = build_corpus(dprs=(args.dpr,), kinds=kinds, degrade=degrade)
    extra = {}
    for path in args.readings_json.split(","):
        if path:
            extra[Path(path).stem] = json.loads(Path(path).read_text())

    totals: Counter[str] = Counter()
    rows = []
    for screen in screens:
        readings = {name: values[screen.screen_id] for name, values in extra.items() if screen.screen_id in values}
        if len(readings) < args.pipeline_minimum:
            continue
        row = classify(screen, readings, minimum=args.pipeline_minimum)
        rows.append(row)
        for key in ("omissions", "merged", "line_missing", "outside_body_band", "other"):
            totals[key] += int(row[key])
    print(json.dumps({"totals": dict(totals), "screens": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
