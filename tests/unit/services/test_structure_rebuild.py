"""Rebuilding a structure tree, and dating the sections in it.

The dating is the part that matters for correspondence. A bound volume of
letters is one document with one date or none; the letters inside it have
hundreds, and a relative date — "yours of the 3d ult." — is relative to the
letter it appears in, not to the volume.
"""

from __future__ import annotations

from research_engine.domain.nodes import build_node_tree
from research_engine.services.ingestion.structure import (
    DATELINE_WINDOW,
    _dated_sections,
)

VOLUME = """# The Civil War Papers

## To Mary Ellen McClellan

Camp near Sharpsburg, Sept. 20, 1862

My dear Nelly, we have had a great battle and I am well.

## To Henry W. Halleck

Head Quarters, Army of the Potomac, March 24 11 am 1862

Dispatch received and understood.

## Editorial note

This section states no date of its own.
"""


def sections_by_heading(text: str) -> dict:
    return {s["heading"]: s for s in _dated_sections(text)}


class TestDatingSections:
    def test_each_letter_carries_its_own_date(self):
        sections = sections_by_heading(VOLUME)

        assert sections["To Mary Ellen McClellan"]["date_start"].startswith("1862-09-20")
        assert sections["To Henry W. Halleck"]["date_start"].startswith("1862-03-24")

    def test_a_section_without_a_date_gets_none(self):
        """Absent, not guessed — a section inherits nothing from its neighbours."""
        assert "date_start" not in sections_by_heading(VOLUME)["Editorial note"]

    def test_a_telegram_time_does_not_become_the_year(self):
        assert sections_by_heading(VOLUME)["To Henry W. Halleck"]["date_start"][:4] == "1862"

    def test_precision_is_recorded(self):
        assert sections_by_heading(VOLUME)["To Mary Ellen McClellan"]["date_precision"] == "day"

    def test_a_date_deep_in_the_body_is_not_the_section_s_date(self):
        """Read far enough into a letter and you find dates it merely mentions."""
        text = (
            "# Papers\n\n## To Someone\n\n"
            + "This letter states no date of its own. " * 12
            + "\n\nBut it mentions May 3, 1863 in passing.\n"
        )
        section = sections_by_heading(text)["To Someone"]
        assert "date_start" not in section
        assert text.index("May 3, 1863") - section["char_start"] > DATELINE_WINDOW

    def test_text_with_no_headings_yields_no_sections(self):
        assert _dated_sections("Just prose, no headings at all.") == []


class TestIntoTheTree:
    def test_dates_reach_the_node_metadata(self):
        """`build_node_tree` folds unrecognised section keys into metadata."""
        drafts = build_node_tree(
            _dated_sections(VOLUME), text_length=len(VOLUME), title="Papers"
        )
        dated = {
            d.title: d.metadata["date_start"]
            for d in drafts
            if d.metadata.get("date_start")
        }

        assert dated["To Mary Ellen McClellan"].startswith("1862-09-20")
        assert dated["To Henry W. Halleck"].startswith("1862-03-24")

    def test_the_tree_still_has_a_root_covering_everything(self):
        drafts = build_node_tree(
            _dated_sections(VOLUME), text_length=len(VOLUME), title="Papers"
        )
        root = drafts[0]

        assert root.node_type == "document"
        assert (root.char_start, root.char_end) == (0, len(VOLUME))

    def test_every_node_span_addresses_the_text(self):
        drafts = build_node_tree(
            _dated_sections(VOLUME), text_length=len(VOLUME), title="Papers"
        )

        for draft in drafts:
            assert 0 <= draft.char_start <= draft.char_end <= len(VOLUME)
