"""Machine claims: what an extraction said a passage asserts, anchored to it.

A machine claim is not a ledger claim. It is one extraction record turned into
a row a later stage can reason over — a statement, who is presented as
speaking, what it is about, how the passage puts it (its modality) — with the
verbatim evidence resolved to source spans, so every row can be checked
against the text it came from. The vocabularies (polarities, modalities, vouch
kinds) are the engine's; what a topic key or a ``*_ref`` means is the profile
layer's, which resolves those later.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic needs it at runtime
from enum import StrEnum
from typing import Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel, Field


class ClaimPolarity(StrEnum):
    """Whether the passage puts the statement forward or against it."""

    affirms = "affirms"
    denies = "denies"


class Modality(StrEnum):
    """How the passage presents the statement.

    A command, a prophecy, and a report of someone else's view cannot be
    weighed as flat assertions, so the presentation travels with the claim
    rather than being flattened away at extraction time.
    """

    asserts = "asserts"
    commands = "commands"
    prophesies = "prophesies"
    reports_other_view = "reports_other_view"
    quotes_scripture = "quotes_scripture"
    hypothetical = "hypothetical"
    question = "question"


class VouchKind(StrEnum):
    """What one text (or voice) is said to do for another's standing."""

    affirms_reliability = "affirms_reliability"
    affirms_authority = "affirms_authority"
    claims_prophesied_by = "claims_prophesied_by"
    self_reliability = "self_reliability"
    affirms_fallibility = "affirms_fallibility"


class TargetForm(StrEnum):
    """Which form of a vouched-for work the vouch is about, if it says."""

    work_as_given = "work_as_given"
    extant_edition = "extant_edition"
    unspecified = "unspecified"


class MachineClaimDraft(BaseModel):
    """A machine claim before it is stored.

    Labels are what the extraction wrote; the ``*_entity_id`` fields are set
    only when a label resolved exactly against the entity store, and the
    ``*_ref`` fields carry a seeded or hint id verbatim for the profile layer
    to resolve later. Nothing here is invented to fill a column.
    """

    run_id: UUID
    extraction_record_id: UUID | None = None
    document_id: UUID
    edition_id: UUID | None = None
    statement: str
    subject_label: str | None = None
    subject_ref: str | None = None
    subject_entity_id: UUID | None = None
    predicate: str | None = None
    object_label: str | None = None
    polarity: ClaimPolarity = ClaimPolarity.affirms
    modality: Modality
    speaker_label: str | None = None
    speaker_ref: str | None = None
    speaker_entity_id: UUID | None = None
    scope_quantifier: str | None = None
    temporal_scope: str | None = None
    topic_keys: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    attributes: dict[str, Any] = Field(default_factory=dict)


class MachineClaim(MachineClaimDraft):
    """One row of `dilemma.claims`."""

    id: UUID
    created_at: datetime


class ClaimEvidenceDraft(BaseModel):
    """One evidence field of a claim, resolved to a source span."""

    claim_id: UUID
    source_span_id: UUID
    field: str
    ordinal: int = Field(ge=0)


class ClaimEvidence(ClaimEvidenceDraft):
    """One row of `dilemma.claim_evidence`."""

    id: UUID


class VouchEdgeDraft(BaseModel):
    """A vouch a claim makes for a target, before it is stored."""

    claim_id: UUID
    run_id: UUID
    kind: VouchKind
    target_label: str | None = None
    target_ref: str | None = None
    target_entity_id: UUID | None = None
    target_edition_id: UUID | None = None
    target_form: TargetForm = TargetForm.unspecified
    attributes: dict[str, Any] = Field(default_factory=dict)


class VouchEdge(VouchEdgeDraft):
    """One row of `dilemma.vouch_edges`."""

    id: UUID
    created_at: datetime


class PairRelation(StrEnum):
    """The verdict of one NLI comparison of two claims."""

    entail = "entail"
    contradict = "contradict"
    neutral = "neutral"


class ClaimEmbedding(BaseModel):
    """One vector for a claim's statement text, under one model version.

    Mirrors `dilemma.claim_embeddings`: keyed by
    ``(claim_id, model, model_version)`` like passage embeddings, so a new
    model version re-embeds rather than overwriting.
    """

    claim_id: UUID
    model: str
    model_version: str
    embedding: list[float]


class PairScoreDraft(BaseModel):
    """One NLI verdict on a claim pair, before it is stored.

    The writer orders the pair (``claim_a_id < claim_b_id`` by string
    compare) so the same pair never lands under two orders; the repository
    enforces that, the draft need only carry the two ids.
    """

    claim_a_id: UUID
    claim_b_id: UUID
    relation: PairRelation
    confidence: float = Field(ge=0, le=1)
    rationale: str
    llm_call_id: UUID | None = None
    model: str
    prompt_hash: str


class PairScore(PairScoreDraft):
    """One row of `dilemma.pair_scores`."""

    run_id: UUID
    created_at: datetime
