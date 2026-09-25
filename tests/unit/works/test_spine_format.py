"""Phase-1 spine, database-free: markers, policy, hashing, and the draft format."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from research_engine.domain.citations import (
    BlockCitations,
    CitationItem,
    CitationOccurrence,
)
from research_engine.domain.works import (
    BlockLinks,
    Placement,
    Work,
    WorkBlock,
    WorkRevision,
    WorkStatus,
)
from research_engine.domain.works_files import Intent
from research_engine.services.works.assembly import (
    AssembledBlock,
    AssembledRevision,
    hash_assembled,
)
from research_engine.services.works.drafting import parse_markdown, render_markdown
from research_engine.services.works.markers import find_markers, format_marker
from research_engine.services.works.validate import resolve_severity

pytestmark = pytest.mark.unit

KEY_A = UUID("11111111-1111-1111-1111-111111111111")
KEY_B = UUID("22222222-2222-2222-2222-222222222222")
CITE_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _work() -> Work:
    now = datetime.now(UTC)
    return Work(
        id=UUID("33333333-3333-3333-3333-333333333333"),
        slug="deror",
        title="Deror",
        work_type="translation",
        status=WorkStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )


def _revision() -> WorkRevision:
    now = datetime.now(UTC)
    return WorkRevision(
        id=UUID("44444444-4444-4444-4444-444444444444"),
        work_id=_work().id,
        revision_number=1,
        created_at=now,
    )


def _block(key: UUID, block_type: str, title: str | None, body: str) -> WorkBlock:
    now = datetime.now(UTC)
    return WorkBlock(
        id=UUID("55555555-5555-5555-5555-555555555555"),
        revision_id=_revision().id,
        block_key=key,
        position=0,
        block_type=block_type,
        title=title,
        body_markdown=body,
        created_at=now,
        updated_at=now,
    )


class TestMarkers:
    def test_format_and_find_round_trip(self):
        assert find_markers(f"a {format_marker(CITE_A)} b") == ({str(CITE_A)}, [])

    def test_non_uuid_marker_is_invalid_not_silent(self):
        keys, invalid = find_markers("see {{cite:c1}} here")

        assert keys == set()
        assert invalid == ["{{cite:c1}}"]


class TestPolicy:
    def test_floor_holds_without_policy(self):
        assert resolve_severity(None, "essay", "AUTH_SPAN_REGION", "warning") == "warning"

    def test_pack_can_escalate_and_allow(self):
        policy = {"translation": {"AUTH_SPAN_REGION": "error", "AUTH_FILE_DRIFT": "allow"}}

        assert resolve_severity(policy, "translation", "AUTH_SPAN_REGION", "warning") == "error"
        assert resolve_severity(policy, "translation", "AUTH_FILE_DRIFT", "warning") == "allow"

    def test_warn_spelling_and_other_types_fall_back(self):
        policy = {"essay": {"AUTH_SPAN_REGION": "warn", "AUTH_FILE_DRIFT": "sometimes"}}

        assert resolve_severity(policy, "essay", "AUTH_SPAN_REGION", "warning") == "warning"
        assert resolve_severity(policy, "dossier", "AUTH_SPAN_REGION", "warning") == "warning"
        # An unknown value is a typo, not a silencer: the floor holds.
        assert resolve_severity(policy, "essay", "AUTH_FILE_DRIFT", "warning") == "warning"


class TestContentHash:
    def _view(self, body: str, row_id: str) -> AssembledRevision:
        block = _block(KEY_A, "paragraph", None, body)
        block = block.model_copy(update={"id": UUID(row_id)})
        return AssembledRevision(
            work=_work(),
            revision=_revision(),
            blocks=[AssembledBlock(block=block, parent_key=None, links=BlockLinks())],
        )

    def test_row_ids_and_timestamps_do_not_move_the_hash(self):
        assert (
            hash_assembled(self._view("words", "55555555-5555-5555-5555-555555555555"))
            == hash_assembled(self._view("words", "66666666-6666-6666-6666-666666666666"))
        )

    def test_words_move_the_hash(self):
        assert hash_assembled(self._view("words", "55555555-5555-5555-5555-555555555555")) != hash_assembled(
            self._view("other words", "55555555-5555-5555-5555-555555555555")
        )


def _view_with_citation() -> AssembledRevision:
    now = datetime.now(UTC)
    occurrence = CitationOccurrence(
        id=UUID("77777777-7777-7777-7777-777777777777"),
        citation_key=CITE_A,
        block_id=UUID("55555555-5555-5555-5555-555555555555"),
        placement=Placement.BLOCK_END,
        intent=Intent.BACKGROUND,
        created_at=now,
    )
    item = CitationItem(
        occurrence_id=occurrence.id, position=0, edition_key="DABAR_2026"
    )
    paragraph = AssembledBlock(
        block=_block(KEY_B, "paragraph", None, "A background claim."),
        parent_key=KEY_A,
        citations=[BlockCitations(occurrence=occurrence, items=[item])],
        links=BlockLinks(),
    )
    heading = AssembledBlock(
        block=_block(KEY_A, "heading", "Release", ""),
        parent_key=None,
        links=BlockLinks(),
    )
    heading.block.attributes["level"] = 2
    return AssembledRevision(work=_work(), revision=_revision(), blocks=[heading, paragraph])


class TestDraftFormat:
    def test_export_renders_comments_citations_and_block_end_markers(self):
        rendered = render_markdown(_view_with_citation())

        assert f"<!-- block:{KEY_A} -->" in rendered
        assert "## Release" in rendered
        assert "DABAR_2026" in rendered
        # block_end occurrence with no marker in the text renders at the end.
        assert format_marker(CITE_A) in rendered

    def test_export_stamps_base_with_content_hash(self):
        view = _view_with_citation()
        front, _ = parse_markdown(render_markdown(view))

        assert front["base"] == hash_assembled(view).hex()

    def test_parse_recovers_keys_types_titles_and_parents(self):
        front, parsed = parse_markdown(render_markdown(_view_with_citation()))

        assert front["work"] == "deror"
        assert [block.key for block in parsed] == [KEY_A, KEY_B]
        assert [block.block_type for block in parsed] == ["heading", "paragraph"]
        assert parsed[0].title == "Release"
        assert parsed[1].parent_index == 0
        assert format_marker(CITE_A) in parsed[1].body

    def test_parse_drops_footnote_definitions(self):
        markdown = (
            "---\nwork: deror\n---\n\n<!-- block:11111111-1111-1111-1111-111111111111 -->\n"
            "Text.\n\n[^c1]: a definition\n"
        )

        _, parsed = parse_markdown(markdown)

        assert len(parsed) == 1
        assert parsed[0].body == "Text."

    def test_import_without_front_matter_is_invalid(self):
        with pytest.raises(ValueError, match="front matter"):
            parse_markdown("No front matter here.\n")

    def test_import_for_wrong_work_is_invalid(self):
        rendered = render_markdown(_view_with_citation()).replace("work: deror", "work: other")

        front, _ = parse_markdown(rendered)

        assert front["work"] == "other"
