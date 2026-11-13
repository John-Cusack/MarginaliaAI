"""The gate, and the three rules the first evaluation got wrong.

Each rule is here because breaking it changed the answer: a gapped page counted as
an unmarked one, the newline between regions counted as an omission, and a table
compared against a truth it can never produce.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from ocr_synth import metrics  # noqa: E402
from wp2_gate import (  # noqa: E402
    GATE,
    clopper_pearson_upper,
    gate_report,
    page_verdict,
    screens_needed_for_gate,
)


def row(screen: str, kind: str, status: str, unmarked: int) -> dict[str, object]:
    return {"screen": screen, "kind": kind, "status": status, "unmarked": unmarked}


def test_a_gapped_pages_errors_are_flagged_not_unmarked():
    """The page is re-taken, so nothing on it is trusted text to be wrong."""
    verdict = page_verdict(row("p1", "two_column_prose", "gapped", 40))
    assert verdict.scored is False
    assert "re-taken" in verdict.reason
    assert verdict.unmarked == 0


def test_table_pages_are_reported_separately_until_wp3_lands():
    verdict = page_verdict(row("t1", "table_page", "marked", 167))
    assert verdict.scored is False
    assert "WP3" in verdict.reason


def test_the_report_says_what_it_excluded_and_why():
    rows = [
        row("a", "two_column_prose", "marked", 0),
        row("b", "two_column_prose", "marked", 3),
        row("c", "two_column_prose", "gapped", 99),
        row("d", "table_page", "marked", 167),
    ]
    report = gate_report(rows)
    assert report["screens"] == 4 and report["scored"] == 2
    assert report["with_an_unmarked_error"] == 1
    assert report["observed"] == 0.5
    assert report["passes"] is False
    assert sum(report["excluded"].values()) == 2
    assert report["by_class"]["two_column_prose"]["screens"] == 2


def test_the_inter_region_join_is_not_an_omission():
    """The stored text is regions joined by a newline; the truth is the page's text.

    Compared after the 7.4 normalization the join disappears, and a page whose only
    difference is the join has no unmarked error at all.
    """
    truth = "the debt was 48.2% and the claim held"
    stored = "the debt was 48.2%\nand the claim held"
    assert metrics.normalize(truth) == metrics.normalize(stored)
    assert metrics.classify_errors(stored, truth), "raw, the join looks like an omission"
    assert not metrics.classify_errors(
        metrics.normalize(stored), metrics.normalize(truth)
    ), "normalized, it does not"


def test_a_gate_under_010_needs_more_than_nineteen_screens():
    """The arithmetic that made the first evaluation's gate unreachable."""
    assert clopper_pearson_upper(0, 19) > GATE, "19 clean screens cannot clear 0.10"
    needed = screens_needed_for_gate()
    assert needed >= 29, needed
    assert clopper_pearson_upper(0, needed) <= GATE
