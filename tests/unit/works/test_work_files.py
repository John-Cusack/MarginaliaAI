"""The file contract parses to the expected models, and one bad entry hides none."""

from __future__ import annotations

import hashlib

import pytest

from research_engine.services.works.files import (
    WorkFileError,
    WorkFileReader,
    parse_work_file,
)

pytestmark = pytest.mark.unit

DOC_ID = "11111111-1111-1111-1111-111111111111"

GOOD = """---
work: W-001
title: "A dabaris fragment"
type: essay
status: draft
created: 2026-09-04
claims: [TEST-001]
citations:
  - id: c1
    document_id: 11111111-1111-1111-1111-111111111111
    char_start: 34
    char_end: 62
    quoted_text: "The prophets pair two words."
    intent: quotation
    edition_key: DABAR_2026
    locator: {page: 1}
  - id: c2
    document_id: 11111111-1111-1111-1111-111111111111
    char_start: 63
    char_end: 117
    quoted_text: "He requires justice"
    intent: background
---

## Notes

Reading [^c1] closely, then [^c2] for context, and [^c1] again.

[^c1]: rendered elsewhere, never parsed as a marker.
[^c9]: a dangling definition is not a marker either.
"""


def _write(tmp_path, name="essay.md", text=GOOD):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestGoodFile:
    def test_parses_to_the_expected_models(self, tmp_path):
        work = parse_work_file(_write(tmp_path), tmp_path)

        assert work.work_path == "essay.md"
        assert work.front_matter.work == "W-001"
        assert work.front_matter.type.value == "essay"
        assert work.front_matter.status.value == "draft"
        assert work.front_matter.claims == ["TEST-001"]
        assert [entry.id for entry in work.front_matter.citations] == ["c1", "c2"]
        assert work.front_matter.citations[0].locator == {"page": 1}
        assert work.entry_errors == []

    def test_sha_is_stable_and_covers_only_the_yaml_block(self, tmp_path):
        first = parse_work_file(_write(tmp_path), tmp_path)
        second = parse_work_file(_write(tmp_path), tmp_path)

        assert first.front_matter_sha == second.front_matter_sha
        raw_block = GOOD.split("\n---\n")[0][len("---\n") :]
        assert first.front_matter_sha == hashlib.sha256(
            raw_block.encode("utf-8")
        ).hexdigest()

    def test_markers_are_found_in_order_and_definitions_excluded(self, tmp_path):
        work = parse_work_file(_write(tmp_path), tmp_path)

        assert work.markers == ["c1", "c2", "c1"]

    def test_reader_lists_works_but_not_the_contract(self, tmp_path):
        _write(tmp_path, "essay.md")
        (tmp_path / "README.md").write_text("# contract\n")
        (tmp_path / "_TEMPLATE.md").write_text("---\n")
        (tmp_path / "_draft.md").write_text("---\n")

        assert WorkFileReader(tmp_path).list_works() == ["essay.md"]


class TestBadEntries:
    BAD = GOOD.replace("    intent: background", "    intent: frobnicate").replace(
        '    quoted_text: "He requires justice"',
        '    quoted_text: ""',
    )

    def test_one_invalid_entry_yields_one_error_and_the_other_parses(
        self, tmp_path
    ):
        work = parse_work_file(_write(tmp_path, text=self.BAD), tmp_path)

        assert [entry.id for entry in work.front_matter.citations] == ["c1"]
        assert len(work.entry_errors) == 1
        assert work.entry_errors[0].citation_id == "c2"

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("  - id: c2\n", "  - id: x1\n"),
            ("    char_start: 34\n", "    char_start: -1\n"),
            ("    char_end: 62\n", "    char_end: 34\n"),  # not after char_start
            (
                "    document_id: 11111111-1111-1111-1111-111111111111\n",
                "    document_id: not-a-uuid\n",
            ),
        ],
    )
    def test_each_malformed_field_is_an_entry_error(self, tmp_path, old, new):
        good_entry = (
            "  - id: c2\n"
            "    document_id: 11111111-1111-1111-1111-111111111111\n"
            "    char_start: 63\n"
            "    char_end: 117\n"
            '    quoted_text: "He requires justice"\n'
            "    intent: background\n"
        )
        bad_entry = (
            "  - id: c2\n"
            "    document_id: 11111111-1111-1111-1111-111111111111\n"
            "    char_start: 34\n"
            "    char_end: 62\n"
            '    quoted_text: "The prophets pair two words."\n'
            "    intent: quotation\n"
        ).replace(old, new)
        text = GOOD.replace(good_entry, bad_entry)
        work = parse_work_file(_write(tmp_path, text=text), tmp_path)

        assert [entry.id for entry in work.front_matter.citations] == ["c1"]
        assert len(work.entry_errors) == 1


class TestBadHeaders:
    @pytest.mark.parametrize(
        "text",
        [
            "no fence at all\n",
            "---\nwork: W-001\n",  # no closing fence
            "---\n- just\n- a\n- list\n---\nbody\n",  # not a mapping
            GOOD.replace("type: essay", "type: pamphlet"),  # bad enum
            GOOD.replace("status: draft", "status: someday"),  # bad enum
            GOOD.replace("work: W-001", "work: W-001\ntitel: typo"),  # unknown key
            GOOD.replace("citations:\n", "citations: {}\n"),  # not a list
        ],
    )
    def test_a_bad_header_is_a_hard_error(self, tmp_path, text):
        with pytest.raises(WorkFileError):
            parse_work_file(_write(tmp_path, text=text), tmp_path)

    def test_a_file_outside_the_works_dir_is_refused(self, tmp_path):
        elsewhere = tmp_path / "elsewhere.md"
        elsewhere.write_text(GOOD, encoding="utf-8")

        with pytest.raises(WorkFileError):
            parse_work_file(elsewhere, tmp_path / "works")
