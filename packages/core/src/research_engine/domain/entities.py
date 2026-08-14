"""Entity and mention domain types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from research_engine.domain.common import MentionSource


class Entity(BaseModel):
    """A canonical entity (person, place, org, etc.)."""

    id: UUID
    entity_type: str
    canonical_name: str
    disambiguator: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class EntityDraft(BaseModel):
    """Data needed to create an entity."""

    entity_type: str
    canonical_name: str
    disambiguator: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class EntityAlias(BaseModel):
    """An alias for an entity."""

    entity_id: UUID
    alias: str
    alias_type: str | None = None


class Mention(BaseModel):
    """A mention of an entity in a passage."""

    id: UUID
    passage_id: UUID
    entity_id: UUID
    span_start: int | None = None
    span_end: int | None = None
    surface_form: str
    confidence: float
    source: MentionSource
    created_at: datetime


class MentionDraft(BaseModel):
    """Data needed to create a mention record."""

    passage_id: UUID
    entity_id: UUID
    span_start: int | None = None
    span_end: int | None = None
    surface_form: str
    confidence: float
    source: MentionSource


class EntityCandidate(BaseModel):
    """A candidate entity from resolution."""

    entity_id: UUID
    canonical_name: str
    entity_type: str
    disambiguator: str | None = None
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    match_score: float
