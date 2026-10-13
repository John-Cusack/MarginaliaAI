"""Where an edition came from, and the ranges in its text that do not nest.

An acquisition is the corpus's record of getting one edition: the method, the
URL, the licence, the raw bytes by digest — or, when it cannot be got, why not
and how it could be. Nothing here is biblical; the values a loader records
(its methods, its licences) are its own.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic needs it at runtime
from enum import StrEnum
from typing import Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel, Field, model_validator


class AcquisitionStatus(StrEnum):
    planned = "planned"
    acquired = "acquired"
    loaded = "loaded"
    blocked = "blocked"
    failed = "failed"


class Collection(StrEnum):
    """Which analyses may see a source.

    `heldout_eval` sources score a result and are never inputs to it — the
    leakage control for a blind test.
    """

    main = "main"
    heldout_eval = "heldout_eval"


class AcquisitionDraft(BaseModel):
    """What a loader declares about an edition before or after fetching it."""

    collection: Collection = Collection.main
    status: AcquisitionStatus = AcquisitionStatus.planned
    method: str | None = None
    url: str | None = None
    license: str | None = None
    license_notes: str | None = None
    retrieved_at: datetime | None = None
    blocked_reason: str | None = None
    suggested_acquisition: str | None = None
    manifest_ref: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _blocked_says_why(self) -> AcquisitionDraft:
        if self.status is AcquisitionStatus.blocked and not self.blocked_reason:
            raise ValueError("a blocked acquisition needs a blocked_reason")
        return self


class Acquisition(AcquisitionDraft):
    """One row of `bibliography.acquisitions`."""

    id: UUID
    edition_id: UUID
    edition_key: str
    created_at: datetime
    updated_at: datetime


class Artifact(BaseModel):
    """One raw file an acquisition fetched, by digest."""

    id: UUID
    acquisition_id: UUID
    sha256: str
    path: str
    bytes: int
    url: str | None = None
    media_type: str | None = None
    retrieved_at: datetime


class CoverageReport(BaseModel):
    """A loader's count of what it loaded against the source's known shape."""

    id: UUID
    acquisition_id: UUID
    unit: str
    expected_units: int | None = None
    loaded_units: int
    anomalies: list[Any] = Field(default_factory=list)
    ok: bool
    checker: str
    checker_version: str
    created_at: datetime


class DocumentMarkDraft(BaseModel):
    """A range over the canonical text that does not nest in the node tree."""

    kind: str
    char_start: int
    char_end: int
    attrs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _span_is_well_formed(self) -> DocumentMarkDraft:
        if self.char_start < 0:
            raise ValueError(f"char_start must be non-negative, got {self.char_start}")
        if self.char_end < self.char_start:
            raise ValueError(
                f"char_end ({self.char_end}) precedes char_start ({self.char_start})"
            )
        return self


class DocumentMark(DocumentMarkDraft):
    id: int
    document_id: UUID
