"""The WP2 gate, evaluated the way the design means it.

Three rules, each of which the first evaluation got wrong, and each of which is a
test here:

* **A gapped page's errors are flagged, not unmarked.** A gapped page is re-taken,
  so nothing on it is stored as trusted text; counting its differences as unmarked
  errors charges the pipeline for a page it refused, which is the opposite of what
  the gate measures.
* **The inter-region join is not an omission.** The stored text is the page's
  regions joined by a newline; the truth is the page's text. Comparing them after
  the §7.4 normalization - whitespace runs collapsed - keeps a join from looking
  like a missing character.
* **Table pages are reported separately, not scored against the whole-table truth.**
  The pipeline reads a table cell by cell, and its canonical text is a table the
  readers never produce; a whole-table comparison measures the mismatch, not the
  reading. Until WP3 lands, tables are reported as WP3-owned and kept out of the
  headline number.

The bound is one-sided 95% Clopper-Pearson on the share of scored screens with an
unmarked error, which is why the *number of clean screens* matters: 19 screens
cannot show a bound under 0.146 however perfect they are, and 29 clean ones are
needed to clear 0.10.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: WP3's classes: a table is read cell by cell and assembled later.
WP3_KINDS = {"table_page", "table_continuation"}
#: The classes the gate is about.
PROSE_KINDS = {
    "chapter_opener",
    "two_column_prose",
    "spanning_heading",
    "sparse_page",
    "split_word_page",
    "location_page",
    "toc_page",
    "notes_page",
    "two_column_notes",
    "figure_page",
    "popup_page",
}
GATE = 0.10


def clopper_pearson_upper(successes: int, trials: int, alpha: float = 0.05) -> float:
    """One-sided 95% upper bound, from the implementation the WP0 tests pinned.

    Not a second copy: a bound computed two ways is a bound nobody can compare, and
    the first copy here got 0/19 -> 0.0027 where the answer is 0.146.
    """
    from ocr_synth.metrics import clopper_pearson_upper as _bound

    return _bound(successes, trials, alpha)


def screens_needed_for_gate(*, clean_share: float = 1.0, gate: float = GATE) -> int:
    """How many screens a bound under the gate needs. Nineteen cannot, ever."""
    for trials in range(1, 500):
        successes = int(trials * (1 - clean_share))
        if clopper_pearson_upper(successes, trials) <= gate:
            return trials
    return -1


@dataclass(frozen=True)
class PageVerdict:
    """One screen's contribution: scored, or excluded with a reason."""

    screen: str
    kind: str
    status: str
    scored: bool
    reason: str
    unmarked: int


def page_verdict(row: dict[str, object]) -> PageVerdict:
    """Is this page's difference count an unmarked-error count, or a flag?"""
    kind = str(row.get("kind", ""))
    status = str(row.get("status", "marked"))
    if kind in WP3_KINDS:
        return PageVerdict(str(row["screen"]), kind, status, False, "WP3-owned: read cell by cell", 0)
    if status == "gapped":
        return PageVerdict(str(row["screen"]), kind, status, False, "gapped: the page is re-taken, so its errors are flagged", 0)
    return PageVerdict(str(row["screen"]), kind, status, True, "", int(row.get("unmarked", 0)))


def gate_report(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    """The gate over a run: per class and overall, with what was excluded and why."""
    verdicts = [page_verdict(row) for row in rows]
    scored = [verdict for verdict in verdicts if verdict.scored]
    bad = sum(1 for verdict in scored if verdict.unmarked)
    excluded = Counter(verdict.reason for verdict in verdicts if not verdict.scored)
    by_class: dict[str, dict[str, object]] = {}
    for verdict in scored:
        entry = by_class.setdefault(verdict.kind, {"screens": 0, "with_an_unmarked_error": 0})
        entry["screens"] = int(entry["screens"]) + 1
        if verdict.unmarked:
            entry["with_an_unmarked_error"] = int(entry["with_an_unmarked_error"]) + 1
    for _kind, entry in by_class.items():
        entry["observed"] = round(int(entry["with_an_unmarked_error"]) / max(1, int(entry["screens"])), 4)
        entry["upper"] = round(
            clopper_pearson_upper(int(entry["with_an_unmarked_error"]), int(entry["screens"])), 4
        )
    return {
        "screens": len(rows),
        "scored": len(scored),
        "with_an_unmarked_error": bad,
        "observed": round(bad / max(1, len(scored)), 4),
        "upper": round(clopper_pearson_upper(bad, len(scored)), 4),
        "gate": GATE,
        "passes": clopper_pearson_upper(bad, len(scored)) <= GATE,
        "excluded": dict(excluded),
        "by_class": by_class,
    }
