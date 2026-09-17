"""Claims, their evidence anchors, and argument-graph edges."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ClaimKind(StrEnum):
    OPPOSITION = "opposition"
    MINE = "mine"
    PREMISE = "premise"
    LEXICAL = "lexical"
    ALLY = "ally"


class ClaimStatus(StrEnum):
    OPEN = "open"
    RESEARCHING = "researching"
    REBUTTED = "rebutted"
    WEAKENED = "weakened"
    UNRESOLVED = "unresolved"
    CONCEDED = "conceded"


class ClaimRelation(StrEnum):
    DEPENDS_ON = "depends_on"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    REFINES = "refines"
    REBUTS = "rebuts"
    CONCEDES = "concedes"
    ENTAILS = "entails"


class AnchorRole(StrEnum):
    ASSERTS = "asserts"
    SUPPORTS = "supports"
    REBUTS = "rebuts"
    CONTEXT = "context"


class AnchorVerifyStatus(StrEnum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    NEAR = "near"

CLAIM_AUDIT_ASSURANCE = (
    "Green means the implemented mechanical checks found no failure. "
    "It does not mean the argument is sound or the source has been interpreted faithfully."
)


class Claim(BaseModel):
    """One addressable proposition in ``argument.claims``."""

    id: UUID
    ref: str
    statement: str
    kind: ClaimKind
    status: ClaimStatus
    confidence: float | None = None
    steelman: str | None = None
    public_ready: bool = False
    academic_candidate: bool = False
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ClaimDraft(BaseModel):
    """Mutable claim fields accepted by the ledger write path."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    statement: str
    kind: ClaimKind
    status: ClaimStatus = ClaimStatus.OPEN
    confidence: float | None = Field(default=None, ge=0, le=1)
    steelman: str | None = None
    public_ready: bool = False
    academic_candidate: bool = False
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ref", "statement")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value


class ClaimEdge(BaseModel):
    id: UUID
    source_id: UUID
    target_id: UUID
    relation: ClaimRelation
    confidence: float | None = None
    note: str | None = None
    created_at: datetime


class ClaimEdgeDraft(BaseModel):
    """An outgoing edge named by the target's stable claim ref."""

    model_config = ConfigDict(extra="forbid")

    target_ref: str
    relation: ClaimRelation
    confidence: float | None = Field(default=None, ge=0, le=1)
    note: str | None = None

    @field_validator("target_ref")
    @classmethod
    def _target_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value


class Anchor(BaseModel):
    id: UUID
    claim_id: UUID
    role: AnchorRole
    person_entity_id: UUID | None = None
    source_span_id: UUID
    quoted_text: str
    verify_status: AnchorVerifyStatus | None = None
    verified_at: datetime | None = None
    parser_version: str | None = None
    edition_id: UUID | None = None
    edition_key: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AnchorDraft(BaseModel):
    """A verified anchor ready for insertion."""

    model_config = ConfigDict(extra="forbid")

    role: AnchorRole
    quoted_text: str
    source_span_id: UUID
    person_entity_id: UUID | None = None
    verify_status: AnchorVerifyStatus
    verified_at: datetime
    parser_version: str | None = None
    edition_id: UUID | None = None
    edition_key: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)

    @field_validator("quoted_text")
    @classmethod
    def _quote_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _assertion_names_a_person(self) -> AnchorDraft:
        if self.role is AnchorRole.ASSERTS and self.person_entity_id is None:
            raise ValueError("an asserts anchor must name a person")
        return self


class AnchorInput(BaseModel):
    """Caller input before quote verification and span resolution."""

    model_config = ConfigDict(extra="forbid")

    role: AnchorRole
    quote: str
    document_id: UUID
    person: str | None = None
    edition_key: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)

    @field_validator("quote")
    @classmethod
    def _input_quote_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _assertion_input_names_a_person(self) -> AnchorInput:
        if self.role is AnchorRole.ASSERTS and not (self.person and self.person.strip()):
            raise ValueError("an asserts anchor must name a person")
        return self

class ClaimFinding(BaseModel):
    rule_id: str
    severity: Literal["error", "warning", "info"]
    claim_ref: str
    message: str
    detail: dict[str, Any] | None = None


class ClaimAuditReport(BaseModel):
    findings: list[ClaimFinding] = Field(default_factory=list)
    checked_refs: list[str] | None = None
    assurance: str




class ClaimWriteResult(BaseModel):
    claim: Claim
    edges: list[ClaimEdge] = Field(default_factory=list)
    anchors: list[Anchor] = Field(default_factory=list)
    findings: list[ClaimFinding] = Field(default_factory=list)
