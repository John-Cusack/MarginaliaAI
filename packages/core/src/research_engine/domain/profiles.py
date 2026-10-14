"""Reader profiles: what each edition is to a reader, and the runs made for one.

A profile is a reader's declared view of the library — which texts bind it,
which it names from outside, which defend it, which criticise it — and over
what dates. The engine keeps each text once and lets every profile give it its
own role. The role vocabulary is the engine's; what a tier or a profile key
*means* is the profile data's.
"""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003 - pydantic needs them at runtime
from decimal import Decimal  # noqa: TC003 - pydantic needs it at runtime
from enum import StrEnum
from typing import Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel, Field, model_validator


class SourceRole(StrEnum):
    """What an edition is to a profile.

    Only `binding` texts are horns of a contradiction from the start. An
    `external` text becomes one only when the run finds a binding text vouching
    for it; `reference`, `defense`, `critical` and `fact` never are.
    """

    binding = "binding"
    external = "external"
    reference = "reference"
    defense = "defense"
    critical = "critical"
    fact = "fact"


class RunStatus(StrEnum):
    running = "running"
    finished = "finished"
    failed = "failed"


class Profile(BaseModel):
    """One row of `dilemma.profiles`."""

    id: UUID
    key: str
    version: int
    profile_hash: str
    title: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    loaded_at: datetime
    created_at: datetime


class RoleDraft(BaseModel):
    """A role a profile gives one edition, before it is stored.

    `valid_from` is inclusive and `valid_to` exclusive; leave either out for an
    open end. `scope` narrows the role to part of the edition by locator
    (`{"books": ["Genesis", ...]}`); empty means the whole edition.
    """

    edition_key: str
    role: SourceRole
    tier: int | None = Field(default=None, ge=1, le=9)
    valid_from: date | None = None
    valid_to: date | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> RoleDraft:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError(
                f"{self.edition_key}: valid_to {self.valid_to} is not after "
                f"valid_from {self.valid_from}"
            )
        return self


class StoredRole(BaseModel):
    """One row of `dilemma.source_roles`, with its edition key."""

    id: UUID
    profile_id: UUID
    edition_id: UUID
    edition_key: str
    role: SourceRole
    tier: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class Run(BaseModel):
    """One row of `dilemma.runs`."""

    id: UUID
    profile_id: UUID
    stage: str
    status: RunStatus
    profile_hash: str
    config: dict[str, Any] = Field(default_factory=dict)
    config_hash: str
    code_version: str | None = None
    model_versions: dict[str, Any] = Field(default_factory=dict)
    items_in: int | None = None
    items_out: int | None = None
    cost_estimate: Decimal | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None
