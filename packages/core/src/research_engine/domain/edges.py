"""Edge/relationship domain types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from research_engine.domain.common import NodeKind


class Edge(BaseModel):
    """A directed edge between two nodes."""

    id: UUID
    source_kind: NodeKind
    source_id: UUID
    target_kind: NodeKind
    target_id: UUID
    relation_type: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_passage_id: UUID | None = None
    confidence: float = 1.0
    created_at: datetime


class EdgeDraft(BaseModel):
    """Data needed to create an edge."""

    source_kind: NodeKind
    source_id: UUID
    target_kind: NodeKind
    target_id: UUID
    relation_type: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_passage_id: UUID | None = None
    confidence: float = 1.0
