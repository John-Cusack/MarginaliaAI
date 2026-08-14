"""Shared domain primitives."""

from __future__ import annotations

import enum
from typing import Annotated
from uuid import UUID

from pydantic import Field


class NodeKind(enum.StrEnum):
    entity = "entity"
    document = "document"
    passage = "passage"
    event = "event"


class DatePrecision(enum.StrEnum):
    day = "day"
    week = "week"
    month = "month"
    season = "season"
    year = "year"
    decade = "decade"


class ExtractionStatus(enum.StrEnum):
    pending = "pending"
    ok = "ok"
    failed = "failed"


class IngestionRunStatus(enum.StrEnum):
    running = "running"
    ok = "ok"
    failed = "failed"
    partial = "partial"


class IngestionItemStatus(enum.StrEnum):
    pending = "pending"
    ok = "ok"
    failed = "failed"
    skipped = "skipped"


class MentionSource(enum.StrEnum):
    llm_extraction = "llm_extraction"
    rule = "rule"
    manual = "manual"


class FusionMode(enum.StrEnum):
    rrf = "rrf"
    weighted = "weighted"
    vector_only = "vector_only"
    keyword_only = "keyword_only"


# Type aliases
EntityId = Annotated[UUID, Field(description="Entity UUID")]
DocumentId = Annotated[UUID, Field(description="Document UUID")]
PassageId = Annotated[UUID, Field(description="Passage UUID")]
EventId = Annotated[UUID, Field(description="Event UUID")]
