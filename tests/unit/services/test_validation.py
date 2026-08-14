"""Tests for extraction evidence validation."""

from __future__ import annotations

from uuid import uuid4

import pytest

from research_engine.domain.errors import EvidenceNotFound
from research_engine.services.extraction.validation import (
    compute_byte_offsets,
    normalize_whitespace,
    validate_evidence_spans,
)


class TestNormalizeWhitespace:
    def test_collapses_spaces(self):
        assert normalize_whitespace("hello   world") == "hello world"

    def test_collapses_newlines(self):
        assert normalize_whitespace("hello\n\nworld") == "hello world"

    def test_strips(self):
        assert normalize_whitespace("  hello  ") == "hello"

    def test_tabs(self):
        assert normalize_whitespace("hello\t\tworld") == "hello world"


class TestValidateEvidenceSpans:
    def test_exact_match(self):
        records = [{"evidence": "the quick brown fox"}]
        passage = "Once upon a time, the quick brown fox jumped over."
        # Should not raise
        validate_evidence_spans(records, passage, uuid4())

    def test_whitespace_fuzzy_match(self):
        records = [{"evidence": "the quick brown fox"}]
        passage = "Once upon a time, the  quick\n brown  fox jumped over."
        validate_evidence_spans(records, passage, uuid4())

    def test_not_found(self):
        records = [{"evidence": "completely different text"}]
        passage = "Once upon a time, the quick brown fox jumped over."
        with pytest.raises(EvidenceNotFound):
            validate_evidence_spans(records, passage, uuid4())

    def test_non_evidence_fields_skipped(self):
        records = [{"topic": "not a substring", "name": "also not"}]
        passage = "Some passage text."
        # Should not raise — only fields with "evidence" in name are checked
        validate_evidence_spans(records, passage, uuid4())

    def test_empty_records(self):
        validate_evidence_spans([], "any text", uuid4())


class TestComputeByteOffsets:
    def test_exact(self):
        result = compute_byte_offsets("quick", "the quick brown fox")
        assert result is not None
        start, end = result
        assert "the quick brown fox"[start:end] == "quick" or start == 4

    def test_not_found(self):
        result = compute_byte_offsets("missing", "the quick brown fox")
        assert result is None

    def test_unicode(self):
        result = compute_byte_offsets("café", "I love café")
        assert result is not None
