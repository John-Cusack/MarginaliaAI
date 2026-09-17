"""Work files — created works as markdown with machine-checked citations.

Phase 0 holds a work as a file, not rows: the front matter carries structured
span citations (`document_id` plus offsets into canonical text) and the body
carries `[^cN]` markers. Later steps move this into `authored.*`; the field
names here are exactly the columns there, so the port is a transcription.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Intent(StrEnum):
    """Why a citation is here — the citation's own vocabulary."""

    QUOTATION = "quotation"
    TRANSLATION = "translation"
    SUPPORT = "support"
    CONTRAST = "contrast"
    BACKGROUND = "background"
    DEFINITION = "definition"
    SOURCE = "source"
    SEE_ALSO = "see_also"


class Role(StrEnum):
    """What a citation does for a claim — the ledger's vocabulary.

    Present only when the citation also backs one of the `claims:` refs.
    """

    ASSERTS = "asserts"
    SUPPORTS = "supports"
    REBUTS = "rebuts"
    CONTEXT = "context"


class WorkType(StrEnum):
    TRANSLATION = "translation"
    ESSAY = "essay"
    DOSSIER = "dossier"
    SCRIPT = "script"
    OUTLINE = "outline"


class WorkStatus(StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    PUBLISHED = "published"


#: Citation ids are `[^cN]` handles: `c` followed by digits, nothing else.
ENTRY_ID_RE = re.compile(r"^c[0-9]+$")


class CitationEntry(BaseModel):
    """One structured span citation from a work's front matter."""

    model_config = ConfigDict(extra="forbid")

    id: str
    intent: Intent
    role: Role | None = None
    document_id: UUID
    char_start: int
    char_end: int
    quoted_text: str
    edition: str | None = None
    edition_key: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_is_a_handle(cls, value: str) -> str:
        if not ENTRY_ID_RE.match(value):
            raise ValueError(f"id must match ^c[0-9]+$, got {value!r}")
        return value

    @field_validator("char_start")
    @classmethod
    def _start_is_an_offset(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"char_start must be non-negative, got {value}")
        return value

    @field_validator("quoted_text")
    @classmethod
    def _quote_is_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("quoted_text must be non-empty")
        return value

    @model_validator(mode="after")
    def _span_is_well_formed(self) -> CitationEntry:
        if self.char_end <= self.char_start:
            raise ValueError(
                f"char_end ({self.char_end}) must exceed "
                f"char_start ({self.char_start})"
            )
        return self


class WorkFrontMatter(BaseModel):
    """The validated header of a work file. Entries that failed validation are
    not here — they are in `WorkFile.entry_errors`, so one bad entry does not
    hide the others."""

    model_config = ConfigDict(extra="forbid")

    work: str
    title: str
    type: WorkType
    status: WorkStatus
    created: date
    claims: list[str] = Field(default_factory=list)
    citations: list[CitationEntry] = Field(default_factory=list)


class EntryError(BaseModel):
    """One front-matter entry that failed validation, with the reason.

    Not an exception: the file still parses, the other entries still verify,
    and `work_verify` reports this as `AUTH_ENTRY_INVALID`.
    """

    citation_id: str | None = None
    message: str


class WorkFile(BaseModel):
    """A parsed work file: header models, body text, and marker handles."""

    work_path: str
    front_matter: WorkFrontMatter
    #: sha256 hex of the raw YAML block bytes, fences excluded. Drift detection
    #: for the Step 4 mirror compares this, not a re-serialization.
    front_matter_sha: str
    body: str
    #: Every `[^cN]` in the body, in order, excluding footnote definition lines.
    markers: list[str] = Field(default_factory=list)
    entry_errors: list[EntryError] = Field(default_factory=list)
