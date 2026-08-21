"""Rebuilding a structure tree, and dating the sections in it.

The dating is the part that matters for correspondence. A bound volume of
letters is one document with one date or none; the letters inside it have
hundreds, and a relative date — "yours of the 3d ult." — is relative to the
letter it appears in, not to the volume.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from research_engine.domain.nodes import build_node_tree
from research_engine.services.ingestion.reindex import ReindexReport, ReindexService
from research_engine.services.ingestion.structure import (
    DATELINE_WINDOW,
    dated_sections,
    node_tree_for,
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
    return {s["heading"]: s for s in dated_sections(text)}


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
        assert dated_sections("Just prose, no headings at all.") == []


class TestIntoTheTree:
    def test_dates_reach_the_node_metadata(self):
        """`build_node_tree` folds unrecognised section keys into metadata."""
        drafts = build_node_tree(
            dated_sections(VOLUME), text_length=len(VOLUME), title="Papers"
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
            dated_sections(VOLUME), text_length=len(VOLUME), title="Papers"
        )
        root = drafts[0]

        assert root.node_type == "document"
        assert (root.char_start, root.char_end) == (0, len(VOLUME))

    def test_every_node_span_addresses_the_text(self):
        drafts = build_node_tree(
            dated_sections(VOLUME), text_length=len(VOLUME), title="Papers"
        )

        for draft in drafts:
            assert 0 <= draft.char_start <= draft.char_end <= len(VOLUME)


class TestOneEntryPointForBothCommands:
    """`reindex chunks` and `reindex structure` must build the same tree.

    They did not. `reindex structure` dated its sections and `reindex chunks`
    did not, and `reindex chunks` deletes the document's nodes before writing
    its own — so re-chunking a volume of correspondence put the tree back with
    every date stripped. On this corpus that was 681 dates on one volume, and
    nothing failed: the node count is identical either way, which is exactly
    why it needed a test rather than a look at the output.
    """

    def test_the_shared_builder_dates_the_sections(self):
        nodes = node_tree_for(VOLUME, title="Papers")
        dated = {n.title: n.metadata.get("date_start") for n in nodes if n.title}
        assert dated["To Mary Ellen McClellan"].startswith("1862-09-20")
        assert dated["To Henry W. Halleck"].startswith("1862-03-24")
        assert dated["Editorial note"] is None

    def test_the_shared_builder_keeps_the_title_on_the_root(self):
        root = node_tree_for(VOLUME, title="Papers")[0]
        assert root.depth == 0
        assert root.title == "Papers"

    async def test_re_chunking_writes_dated_nodes(self):
        """Drive the reindex path itself, not just the function it should call."""
        written: list = []

        nodes_repo = AsyncMock()
        nodes_repo.insert_many = AsyncMock(
            side_effect=lambda tx, doc_id, drafts: written.extend(drafts) or []
        )
        service = ReindexService(
            engine=MagicMock(),
            passage_repo=AsyncMock(),
            document_text_repo=AsyncMock(),
            embedding=AsyncMock(),
            document_node_repo=nodes_repo,
        )

        tx = MagicMock()
        tx.conn.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=lambda: "Papers")
        )
        await service._rebuild_nodes(tx, uuid4(), VOLUME, [], ReindexReport())

        dated = [n for n in written if n.metadata.get("date_start")]
        assert len(dated) == 2, "the two letters must keep their datelines"
        assert written[0].title == "Papers", "the root must keep the document title"
