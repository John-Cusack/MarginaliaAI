"""Authored works — the Phase 1 spine.

A work is rows from its first freeze: revisions own ordered blocks, blocks own
citation occurrences and links. Drafts validate before the database does; rows
are what came back.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkStatus(StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class RevisionState(StrEnum):
    DRAFT = "draft"
    FROZEN = "frozen"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class Placement(StrEnum):
    INLINE = "inline"
    BLOCK_END = "block_end"


class Edition(BaseModel):
    """One row of `bibliography.editions`: a Zotero key seen at ingest."""

    id: UUID
    zotero_key: str
    csl: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Work(BaseModel):
    id: UUID
    slug: str
    title: str
    work_type: str
    status: WorkStatus = WorkStatus.DRAFT
    language: str | None = None
    abstract: str | None = None
    current_revision_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class WorkDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str
    work_type: str
    language: str | None = None
    abstract: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("slug", "title")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value


class WorkRevision(BaseModel):
    id: UUID
    work_id: UUID
    revision_number: int
    parent_revision_id: UUID | None = None
    state: RevisionState = RevisionState.DRAFT
    message: str | None = None
    content_hash: bytes | None = None
    created_by: str = "user"
    created_at: datetime
    frozen_at: datetime | None = None
    published_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkRevisionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_id: UUID
    revision_number: int = 1
    parent_revision_id: UUID | None = None
    message: str | None = None
    created_by: str = "user"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("revision_number")
    @classmethod
    def _number_starts_at_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("revision_number starts at 1")
        return value


class WorkBlock(BaseModel):
    id: UUID
    revision_id: UUID
    block_key: UUID
    parent_id: UUID | None = None
    position: int
    block_type: str
    title: str | None = None
    body_markdown: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class WorkBlockDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: UUID
    block_key: UUID
    parent_id: UUID | None = None
    position: int = 0
    block_type: str
    title: str | None = None
    body_markdown: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("position")
    @classmethod
    def _position_is_an_order(cls, value: int) -> int:
        if value < 0:
            raise ValueError("position is a 0-based order, never negative")
        return value


class BlockSourceLink(BaseModel):
    block_id: UUID
    source_span_id: UUID
    relation: str
    confidence: float | None = None
    note: str | None = None
    created_at: datetime


class BlockSourceLinkDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    source_span_id: UUID
    relation: str
    confidence: float | None = None
    note: str | None = None

    @field_validator("confidence")
    @classmethod
    def _confidence_is_a_fraction(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 1:
            raise ValueError("confidence must be between 0 and 1")
        return value


class BlockEntityLink(BaseModel):
    block_id: UUID
    entity_id: UUID
    relation: str
    surface_form: str | None = None
    created_at: datetime


class BlockLinks(BaseModel):
    """Both link kinds of one block, for get and trace."""

    sources: list[BlockSourceLink] = Field(default_factory=list)
    entities: list[BlockEntityLink] = Field(default_factory=list)


class BlockEntityLinkDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: UUID
    entity_id: UUID
    relation: str
    surface_form: str | None = None


class Waiver(BaseModel):
    """A row that lets a gated finding pass: who, what, why."""

    id: UUID
    revision_id: UUID
    rule_id: str
    subject: str | None = None
    actor: str
    reason: str
    created_at: datetime


class WaiverDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: UUID
    rule_id: str
    subject: str | None = None
    actor: str = "user"
    reason: str

    @field_validator("rule_id", "reason")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _actor_is_named(self) -> WaiverDraft:
        if not self.actor.strip():
            raise ValueError("a waiver records who, not just why")
        return self
