#!/usr/bin/env python3
"""Consensus auditors: a difference counts only when two readers of different
families say the same thing at the same place.

An audit reader that transcribes and character-diffs its own transcript reports a
great many differences on text that is already right - a case, a punctuation
mark, a ligature, a re-wrapped line. Measured singly, every one of those is a
false alarm that sends the span to adjudication. Two independent readers rarely
make the same mistake in the same place, so requiring agreement between two of
them should remove most of that noise while keeping the real errors: where both
read the truth and the stored text is wrong, both still report the truth.

The definition, throughout, is design 7.4's: differences are compared after the
normalization the aligner uses - Unicode NFC, ligatures expanded, whitespace runs
collapsed to one space. Case, punctuation, signs, decimals and accents remain
significant. A difference is a finding of a planted error only when the reading
at the span equals the truth after that normalization.

  auditor_consensus.py --dpr 3 \\
      --readers-json a.json,b.json,c.json --reader-stem vlm \\
      --transcript florence=/tmp/readings_florence_3x.json \\
      --transcript internvl=/tmp/readings_internvl_3x.json \\
      --transcript gemma=/tmp/readings_gemma_3x.json \\
      --claude-differences /tmp/claude_readings.json \\
      --json /tmp/consensus.json
"""

from __future__ import annotations

import argparse
import json
import sys
from math import comb
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import pipeline_errors  # noqa: E402
from ocr_synth import metrics  # noqa: E402
from ocr_synth.corpus import Screen, build_corpus  # noqa: E402
from ocr_synth.seeded_errors import build_seeded, can_carry  # noqa: E402

SYNTHETIC_COUNTS = {
    "real_word": 1, "digit": 1, "omission": 1, "insertion": 1, "punctuation": 1, "order": 1,
}
TABLE_KINDS = ("table_page", "table_continuation")
DERIVED = {"table_page", "table_continuation", "toc_page", "figure_page"}
Readings = list[tuple[int, int, str]]  # (start, end, reading) in candidate coordinates


def normalize(text: str) -> str:
    return metrics.normalize(text)


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def load_transcript(path: str) -> dict[str, str]:
    raw = json.loads(Path(path).read_text())
    joined: dict[str, str] = {}
    for key, text in raw.items():
        screen, _, tail = key.partition("__r")
        if tail:
            joined[screen] = (joined.get(screen, "") + "\n\n" + text).strip()
        else:
            joined[key] = text
    return joined


def transcript_edits(joined: dict[str, str]) -> Callable[[Screen, str], Readings]:
    """Differences between the model's transcript and the stored text, by screen."""

    def edits(screen: Screen, final: str) -> Readings:
        text = joined.get(screen.screen_id, "")
        return [
            (edit.final_start, edit.final_end, edit.truth_text)
            for edit in metrics.classify_errors(text, final)
        ]

    return edits


def dumped_edits(dump: dict[str, list[dict[str, object]]]) -> Callable[[Screen, str], Readings]:
    """Differences a region auditor reported, by screen, in candidate coordinates."""

    def edits(screen: Screen, final: str) -> Readings:
        return [
            (int(entry["start"]), int(entry["end"]), str(entry.get("reading") or ""))
            for entry in dump.get(screen.screen_id, [])
        ]

    return edits


def _agrees(a: tuple[int, int, str], b: tuple[int, int, str]) -> bool:
    return bool(a[2]) and bool(b[2]) and overlaps((a[0], a[1]), (b[0], b[1])) and normalize(a[2]) == normalize(b[2])


def _reading_at(span: tuple[int, int], final: str) -> str:
    return final[span[0]:span[1]]


def consensus_edits(members: Sequence[Readings], final: str, minimum: int = 2) -> Readings:
    """Differences at least ``minimum`` members agree on, in candidate coordinates.

    Agreement means the same reading after normalization at overlapping spans, and
    the reading differs from what the stored text says there - otherwise it is not
    a difference at all.
    """
    flat = [entry for member in members for entry in member]
    grouped: list[Readings] = []
    for entry in flat:
        placed = False
        for group in grouped:
            if _agrees(entry, group[0]):
                group.append(entry)
                placed = True
                break
        if not placed:
            grouped.append([entry])
    out: Readings = []
    for group in grouped:
        if len(group) < minimum:
            continue
        start = min(e[0] for e in group)
        end = max(e[1] for e in group)
        reading = group[0][2]
        if normalize(reading) == normalize(_reading_at((start, end), final)):
            continue
        out.append((start, end, reading))
    return out


def strict_hit(
    errors, edits: Readings, final: str, containment: bool = False
) -> tuple[int, list[dict[str, object]]]:
    """How many planted errors a set of edits finds, under design 7.4's definition."""
    found = 0
    detail = []
    for error in errors:
        hit = any(
            overlaps((e[0], e[1]), error.final_span)
            and reading_reads(e[2], error.truth_text, containment)
            for e in edits
        )
        found += 1 if hit else 0
        detail.append(
            {
                "kind": error.kind,
                "truth": error.truth_text,
                "candidate": error.final_text,
                "found": hit,
            }
        )
    return found, detail


def by_kind(rows: Iterable[dict[str, object]]) -> dict[str, list[int]]:
    counts: dict[str, list[int]] = {}
    for row in rows:
        seen, found = counts.setdefault(str(row["kind"]), [0, 0])
        counts[str(row["kind"])] = [seen + 1, found + (1 if row["found"] else 0)]
    return counts


def clopper_pearson_lower(k: int, n: int, alpha: float = 0.05) -> float:
    if k == 0:
        return 0.0
    low, high = 0.0, 1.0
    for _ in range(100):
        middle = (low + high) / 2
        tail = sum(comb(n, i) * middle**i * (1 - middle) ** (n - i) for i in range(k, n + 1))
        low, high = (low, middle) if tail > alpha else (middle, high)
    return low


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dpr", type=int, default=3)
    parser.add_argument("--kinds", default=None)
    parser.add_argument("--readers-json", required=True, help="comma-separated reader readings")
    parser.add_argument("--reader-stem", default="vlm", help="which reader file is the VLM")
    parser.add_argument("--pipeline-minimum", type=int, default=3)
    parser.add_argument("--transcript", action="append", default=[], help="NAME=PATH of a local transcript")
    parser.add_argument("--claude-differences", default=None)
    parser.add_argument("--json", default=None)
    parser.add_argument(
        "--containment",
        action="store_true",
        help="a detection counts when the reader's span contains the truth's words, not only "
        "when the reading equals it: audit readers report wider spans than the error",
    )
    parser.add_argument(
        "--include-derived",
        action="store_true",
        help="keep table, TOC and figure pages in the population (their canonical text is "
        "derived, so the pipeline-shaped set normally drops them)",
    )
    args = parser.parse_args(argv)

    screens = build_corpus(dprs=(args.dpr,), kinds=tuple(args.kinds.split(",")) if args.kinds else None)
    readers: dict[str, dict[str, str]] = {}
    for path in args.readers_json.split(","):
        if path:
            readers[Path(path).stem] = json.loads(Path(path).read_text())
    if args.reader_stem in readers:
        readers["vlm"] = readers.pop(args.reader_stem)

    auditors: dict[str, Callable[[Screen, str], Readings]] = {}
    for spec in args.transcript:
        name, _, path = spec.partition("=")
        auditors[name] = transcript_edits(load_transcript(path))
    if args.claude_differences:
        auditors["claude"] = dumped_edits(json.loads(Path(args.claude_differences).read_text()))

    # --- the population: the readers' agreed mistakes on page-visible text ----
    built = []
    for screen in screens:
        if screen.kind in DERIVED and not args.include_derived:
            continue
        readings = {name: values[screen.screen_id] for name, values in readers.items() if screen.screen_id in values}
        if len(readings) < 2:
            continue
        page, agreed = pipeline_errors.build_pipeline_set(
            screen.truth_text, readings, minimum=args.pipeline_minimum
        )
        if not page.errors:
            continue
        # page.errors are the planted spans, one per agreed error except a join,
        # which is one fused span for two words. Detection is measured against
        # those spans, because a span is what an auditor can report on.
        built.append((screen, page, page.errors))

    population = []
    for screen, page, errors in built:
        population.append(
            {
                "screen": screen.screen_id,
                "kind": screen.kind,
                "final": page.final,
                "errors": errors,
                "edits": {name: fn(screen, page.final) for name, fn in auditors.items()},
            }
        )

    # --- options: single auditors, pairs across families, and the majority ---
    options: dict[str, Callable[[dict[str, object]], Readings]] = {
        name: (lambda row, name=name: row["edits"][name]) for name in auditors
    }
    names = list(auditors)
    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :] if "claude" not in (a, b)]
    for a, b in pairs:
        options[f"{a}+{b}"] = (lambda row, a=a, b=b: consensus_edits([row["edits"][a], row["edits"][b]], row["final"]))
    local = [n for n in names if n != "claude"]
    if len(local) >= 3:
        options[f"majority of {len(local)}"] = (
            lambda row: consensus_edits([row["edits"][n] for n in local], row["final"])
        )

    population_rows = list(population)
    errors_all = [error for row in population for error in row["errors"]]
    results: dict[str, object] = {
        "population": {
            "screens": [row["screen"] for row in population],
            "errors": len(errors_all),
            "by_kind": {
                kind: len([error for error in errors_all if error.kind == kind])
                for kind in sorted({error.kind for error in errors_all})
            },
            "auditors": names,
        },
        "options": {},
    }

    for label, choose in options.items():
        found_by_kind: dict[str, list[int]] = {}
        planted = found = 0
        for row in population:
            chosen = choose(row)
            hit, _ = strict_hit(row["errors"], chosen, row["final"], containment=args.containment)
            found += hit
            planted += len(row["errors"])
            for error in row["errors"]:
                seen, caught = found_by_kind.setdefault(error.kind, [0, 0])
                found_by_kind[error.kind] = [
                    seen + 1, caught + (1 if _hit_one(error, chosen, args.containment) else 0)
                ]
        results["options"][label] = {
            "agreed": {
                "planted": planted,
                "detected": found,
                "s_L": round(clopper_pearson_lower(found, planted), 4),
                "by_kind": {
                    kind: {
                        "n": seen,
                        "detected": caught,
                        "s_L": round(clopper_pearson_lower(caught, seen), 4),
                    }
                    for kind, (seen, caught) in sorted(found_by_kind.items())
                },
            }
        }

    # --- planted and tables, and clean-text false alarms ----------------------
    for label, choose in options.items():
        for section, counts, kinds in (
            ("planted", SYNTHETIC_COUNTS, None),
            ("tables", SYNTHETIC_COUNTS, TABLE_KINDS),
        ):
            usable = [s for s in screens if can_carry(s.truth_text, counts)]
            if kinds:
                usable = [s for s in usable if s.kind in kinds]
            if not usable:
                continue
            seeded = build_seeded(usable, counts)
            planted = found = 0
            found_by_kind: dict[str, list[int]] = {}
            for screen, page in zip(usable, seeded, strict=True):
                chosen = choose({"screen": screen.screen_id, "kind": screen.kind, "final": page.final,
                                 "errors": page.errors, "edits": {n: fn(screen, page.final) for n, fn in auditors.items()}})
                planted += len(page.errors)
                for error in page.errors:
                    hit = _hit_one(error, chosen, args.containment)
                    found += 1 if hit else 0
                    seen, caught = found_by_kind.setdefault(error.kind, [0, 0])
                    found_by_kind[error.kind] = [seen + 1, caught + (1 if hit else 0)]
            results["options"][label][section] = {
                "pages": len(usable),
                "planted": planted,
                "detected": found,
                "s_L": round(clopper_pearson_lower(found, planted), 4),
                "by_kind": {
                    kind: {"n": seen, "detected": caught, "s_L": round(clopper_pearson_lower(caught, seen), 4)}
                    for kind, (seen, caught) in sorted(found_by_kind.items())
                },
            }

    for label, choose in options.items():
        per_screen = []
        for screen in screens:
            chosen = choose({"screen": screen.screen_id, "kind": screen.kind, "final": screen.truth_text,
                             "errors": (), "edits": {n: fn(screen, screen.truth_text) for n, fn in auditors.items()}})
            per_screen.append(len(chosen))
        total = sum(per_screen)
        results["options"][label]["clean"] = {
            "pages": len(screens),
            "differences": total,
            "per_screen": round(total / len(screens), 3),
            "screens_with_one": sum(1 for n in per_screen if n),
            "expected_per_60_screens": round(total / len(screens) * 60, 1),
        }

    text = json.dumps(results, indent=1)
    if args.json:
        Path(args.json).write_text(text)
    population = results["population"]
    print(f"population: {population['errors']} agreed errors on {len(population['screens'])} screens")
    for kind, n in population["by_kind"].items():
        print(f"  {kind:22s} n={n}")
    per_screen = {
        row["screen"]: len(row["errors"]) for row in population_rows if row["errors"]
    }
    print(f"  errors per screen: {per_screen}")
    for label, block in results["options"].items():
        agreed, clean = block["agreed"], block["clean"]
        print(
            f"{label:26s} agreed {agreed['detected']}/{agreed['planted']} s_L={agreed['s_L']:.4f} | "
            f"planted {block.get('planted', {}).get('detected', 0)}/{block.get('planted', {}).get('planted', 0)} | "
            f"tables {block.get('tables', {}).get('detected', 0)}/{block.get('tables', {}).get('planted', 0)} | "
            f"false alarms {clean['per_screen']}/screen ({clean['expected_per_60_screens']}/60 screens)"
        )
    return 0


def _tokens(text: str) -> list[str]:
    return normalize(text).split(" ")


def reading_reads(reading: str, truth: str, containment: bool) -> bool:
    """Does this reading say what the truth says at the span?

    ``containment`` exists because of a real property of audit readers: a difference
    is reported over the span the reader compared, and that span is often *wider*
    than the stored error. Claude answering for the span storing '-1.5%' says "and
    −1.5% is (minus sign, not ..." - it has read the truth and is explaining it, but
    its reading is not equal to the one word. Equality is the definition design 7.4
    implies for a character-level aligner; containment is what tests whether the
    reader got the span right when its answer is a sentence. Both are reported,
    because they answer different questions.
    """
    if not reading:
        return False
    if not containment:
        return normalize(reading) == normalize(truth)
    wanted, said = _tokens(truth), _tokens(reading)
    if not wanted:
        return False
    return any(
        said[i : i + len(wanted)] == wanted for i in range(len(said) - len(wanted) + 1)
    )


def _hit_one(error, edits: Readings, containment: bool = False) -> bool:
    return any(
        overlaps((e[0], e[1]), error.final_span)
        and reading_reads(e[2], error.truth_text, containment)
        for e in edits
    )


if __name__ == "__main__":
    raise SystemExit(main())
