"""Tests for extraction evidence validation.

The offsets these produce are what makes an extracted claim checkable, so the
assertions here are deliberately about the *text at the offsets* rather than
about the offsets being merely present.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from research_engine.domain.errors import EvidenceNotFound, ValidationError
from research_engine.services.extraction.validation import (
    locate_span,
    normalize_whitespace,
    validate_records,
)

RECORD_TYPES = {
    "claim": {
        "id": "claim",
        "fields": {
            "assertion": {"type": "string", "required": True},
            "quote": {"type": "evidence_span", "required": True},
        },
    }
}

PASSAGE = "Once upon a time, the quick brown fox jumped over the lazy dog."


def record(**fields):
    return {"record_type": "claim", "fields": fields}


class TestNormalizeWhitespace:
    def test_collapses_spaces(self):
        assert normalize_whitespace("hello   world") == "hello world"

    def test_collapses_newlines(self):
        assert normalize_whitespace("hello\n\nworld") == "hello world"

    def test_strips(self):
        assert normalize_whitespace("  hello  ") == "hello"

    def test_tabs(self):
        assert normalize_whitespace("hello\t\tworld") == "hello world"


class TestLocateSpan:
    def test_offsets_index_the_original_text(self):
        span = locate_span("quick brown fox", PASSAGE)
        assert span is not None
        start, end = span
        assert PASSAGE[start:end] == "quick brown fox"

    def test_whitespace_differences_still_index_the_original(self):
        """The tolerance is in the pattern, never in the text being searched.

        Searching a whitespace-collapsed copy and returning offsets into *that*
        is wrong by however much whitespace preceded the match — a citation that
        verifies against nothing. Here the passage is hard-wrapped and the quote
        is not, which is exactly what a model re-typing a quotation produces.
        """
        passage = "Once upon a time, the quick\n   brown  fox jumped over."
        span = locate_span("the quick brown fox", passage)
        assert span is not None
        start, end = span
        assert passage[start:end] == "the quick\n   brown  fox"
        assert normalize_whitespace(passage[start:end]) == "the quick brown fox"

    def test_not_found(self):
        assert locate_span("completely different text", PASSAGE) is None

    def test_empty(self):
        assert locate_span("   ", PASSAGE) is None

    def test_unicode_offsets_are_characters_not_bytes(self):
        passage = "I love café society"
        span = locate_span("café", passage)
        assert span is not None
        start, end = span
        assert passage[start:end] == "café"


class TestValidateRecords:
    def test_anchors_the_evidence(self):
        [result] = validate_records(
            [record(assertion="foxes jump", quote="the quick brown fox")],
            PASSAGE,
            uuid4(),
            RECORD_TYPES,
        )
        assert result.record_type == "claim"
        assert PASSAGE[result.evidence_start : result.evidence_end] == (
            "the quick brown fox"
        )

    def test_invented_evidence_is_rejected(self):
        with pytest.raises(EvidenceNotFound):
            validate_records(
                [record(assertion="x", quote="a sentence never written")],
                PASSAGE,
                uuid4(),
                RECORD_TYPES,
            )

    def test_unknown_record_type_is_rejected(self):
        with pytest.raises(ValidationError, match="record_type"):
            validate_records(
                [{"record_type": "invented", "fields": {}}],
                PASSAGE,
                uuid4(),
                RECORD_TYPES,
            )

    def test_missing_required_field_is_rejected(self):
        with pytest.raises(ValidationError, match="assertion"):
            validate_records(
                [record(quote="the quick brown fox")], PASSAGE, uuid4(), RECORD_TYPES
            )

    def test_a_record_that_quotes_nothing_is_rejected(self):
        """The failure the old validator could not see.

        It looked for fields whose *name* contained "evidence", so a schema
        naming its quotation `quote` was never checked at all, and a record with
        no quotation passed silently. Evidence fields are found by their
        declared type now.
        """
        types = {
            "claim": {
                "id": "claim",
                "fields": {
                    "assertion": {"type": "string"},
                    "quote": {"type": "evidence_span"},
                },
            }
        }
        with pytest.raises(ValidationError, match="quoted nothing"):
            validate_records(
                [record(assertion="unsupported")], PASSAGE, uuid4(), types
            )

    def test_record_type_declaring_no_evidence_field_is_rejected(self):
        types = {"claim": {"id": "claim", "fields": {"assertion": {"type": "string"}}}}
        with pytest.raises(ValidationError, match="evidence_span"):
            validate_records(
                [record(assertion="anything")], PASSAGE, uuid4(), types
            )

    def test_fields_must_be_an_object(self):
        with pytest.raises(ValidationError, match="fields"):
            validate_records(
                [{"record_type": "claim", "fields": "not an object"}],
                PASSAGE,
                uuid4(),
                RECORD_TYPES,
            )

    def test_empty_records(self):
        assert validate_records([], PASSAGE, uuid4(), RECORD_TYPES) == []
