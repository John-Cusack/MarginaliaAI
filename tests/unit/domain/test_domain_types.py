"""Tests for domain types — pure data model validation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from research_engine.domain.common import (
    DatePrecision,
    ExtractionStatus,
    FusionMode,
    NodeKind,
)
from research_engine.domain.documents import Document, DocumentDraft
from research_engine.domain.edges import EdgeDraft
from research_engine.domain.entities import (
    Entity,
)
from research_engine.domain.events import (
    FuzzyDate,
)
from research_engine.domain.extractions import (
    Extraction,
    ExtractionOptions,
    ExtractionResult,
)
from research_engine.domain.passages import (
    PassageDraft,
    SearchFilters,
    SearchQuery,
    SearchResult,
)


class TestNodeKind:
    def test_values(self):
        assert NodeKind.entity == "entity"
        assert NodeKind.document == "document"
        assert NodeKind.passage == "passage"
        assert NodeKind.event == "event"


class TestDatePrecision:
    def test_values(self):
        assert DatePrecision.day == "day"
        assert DatePrecision.decade == "decade"


class TestDocumentDraft:
    def test_defaults(self):
        draft = DocumentDraft(
            source="/path/to/file.txt",
            content_hash=b"abc123",
            parser="plain_text",
            parser_version="1.0",
        )
        assert draft.document_type == "generic"
        assert draft.metadata == {}
        assert draft.title is None

    def test_full(self):
        draft = DocumentDraft(
            title="A Letter",
            document_type="letter",
            language="en",
            source="/path/to/file.txt",
            content_hash=b"abc123",
            parser="plain_text",
            parser_version="1.0",
            metadata={"sender": "McClellan"},
        )
        assert draft.title == "A Letter"
        assert draft.metadata["sender"] == "McClellan"


class TestDocument:
    def test_roundtrip(self):
        doc = Document(
            id=uuid4(),
            title="Test",
            document_type="letter",
            language="en",
            source="/test.txt",
            content_hash=b"hash",
            parser="plain_text",
            parser_version="1.0",
            ingested_at=datetime.now(UTC),
            metadata={"key": "value"},
        )
        data = doc.model_dump()
        doc2 = Document(**data)
        assert doc2.id == doc.id
        assert doc2.metadata == {"key": "value"}


class TestPassageDraft:
    def test_minimal(self):
        draft = PassageDraft(
            position=0,
            char_start=0,
            char_end=11,
            text="Hello world",
            chunker="prose_window",
            chunker_version="2.0",
        )
        assert draft.token_count is None
        assert draft.metadata == {}

    def test_span_is_required(self):
        """A passage with no address cannot be cited, verified, or re-anchored."""
        with pytest.raises(ValidationError):
            PassageDraft(
                position=0,
                text="Hello world",
                chunker="prose_window",
                chunker_version="2.0",
            )

    def test_span_width_must_match_text_length(self):
        """Catches the `.strip()` bug at construction rather than at citation time."""
        with pytest.raises(ValidationError, match="does not match text length"):
            PassageDraft(
                position=0,
                char_start=0,
                char_end=20,
                text="Hello world",
                chunker="prose_window",
                chunker_version="2.0",
            )

    def test_reversed_span_is_rejected(self):
        with pytest.raises(ValidationError, match="precedes char_start"):
            PassageDraft(
                position=0,
                char_start=30,
                char_end=10,
                text="",
                chunker="prose_window",
                chunker_version="2.0",
            )

    def test_negative_start_is_rejected(self):
        with pytest.raises(ValidationError, match="non-negative"):
            PassageDraft(
                position=0,
                char_start=-1,
                char_end=10,
                text="Hello worl",
                chunker="prose_window",
                chunker_version="2.0",
            )


class TestSearchQuery:
    def test_defaults(self):
        q = SearchQuery(text="test query")
        assert q.k == 20
        assert q.fusion_mode == FusionMode.rrf
        assert q.rerank is True

    def test_filters(self):
        q = SearchQuery(
            text="McClellan letters",
            filters=SearchFilters(
                document_types=["letter"],
                date_range_start="1862-01-01",
            ),
        )
        assert q.filters.document_types == ["letter"]


class TestSearchResult:
    def test_empty(self):
        result = SearchResult(hits=[], total_candidates=0)
        assert len(result.hits) == 0


class TestEntity:
    def test_create(self):
        entity = Entity(
            id=uuid4(),
            entity_type="person",
            canonical_name="George B. McClellan",
            disambiguator="general",
            attributes={"birth_year": 1826},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert entity.canonical_name == "George B. McClellan"
        assert entity.disambiguator == "general"


class TestFuzzyDate:
    def test_create(self):
        fd = FuzzyDate(
            start=datetime(1862, 7, 1, tzinfo=UTC),
            end=datetime(1862, 8, 31, tzinfo=UTC),
            precision=DatePrecision.season,
        )
        assert fd.precision == DatePrecision.season


class TestExtractionResult:
    def test_from_cached(self):
        extraction = Extraction(
            id=uuid4(),
            passage_id=uuid4(),
            schema_id=uuid4(),
            extractor_version="1.0",
            llm_model="claude-sonnet-4-5-20250929",
            status=ExtractionStatus.ok,
            records=[{"type": "test"}],
            created_at=datetime.now(UTC),
        )
        result = ExtractionResult.from_cached(extraction)
        assert result.from_cache is True
        assert result.status == ExtractionStatus.ok


class TestEdgeDraft:
    def test_create(self):
        draft = EdgeDraft(
            source_kind=NodeKind.document,
            source_id=uuid4(),
            target_kind=NodeKind.document,
            target_id=uuid4(),
            relation_type="replies_to",
        )
        assert draft.confidence == 1.0


class TestExtractionOptions:
    def test_defaults(self):
        opts = ExtractionOptions()
        assert opts.force_refresh is False
        assert opts.concurrency == 8
        assert opts.caller == "core"
