"""Provenance and operations domain types."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from research_engine.domain.common import IngestionItemStatus, IngestionRunStatus


class LLMCall(BaseModel):
    """A logged LLM call for auditability and cost tracking."""

    id: UUID
    purpose: str
    caller: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_estimate: float | None = None
    duration_ms: int | None = None
    status: str
    error: str | None = None
    created_at: datetime


class LLMCallDraft(BaseModel):
    """Data needed to log an LLM call."""

    purpose: str
    caller: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_estimate: float | None = None
    duration_ms: int | None = None
    status: str
    error: str | None = None


class UsageGroup(BaseModel):
    """Spend and token counts for one combination of grouping keys."""

    key: dict[str, str]
    calls: int
    input_tokens: int
    output_tokens: int
    cost: float
    errors: int


class UsageSummary(BaseModel):
    """Aggregated LLM spend over a time window."""

    since: datetime | None = None
    until: datetime | None = None
    group_by: list[str]
    groups: list[UsageGroup]
    total_calls: int
    total_cost: float


class BudgetExceeded(Exception):
    """Configured LLM spend limit reached; the call was refused, not attempted."""

    def __init__(self, spent: float, limit: float, window_days: int) -> None:
        self.spent = spent
        self.limit = limit
        self.window_days = window_days
        super().__init__(
            f"LLM budget exceeded: ${spent:.2f} spent in the last {window_days}d "
            f"against a ${limit:.2f} limit. Raise RE_LLM_BUDGET_USD or wait for "
            f"the window to roll over."
        )


class IngestionRun(BaseModel):
    """A batch ingestion run record."""

    id: UUID
    started_at: datetime
    completed_at: datetime | None = None
    source_spec: dict[str, Any] = Field(default_factory=dict)
    status: IngestionRunStatus
    stats: dict[str, Any] = Field(default_factory=dict)


class IngestionItem(BaseModel):
    """A single item within an ingestion run."""

    id: UUID
    run_id: UUID
    source_ref: str
    document_id: UUID | None = None
    status: IngestionItemStatus
    error: str | None = None
    duration_ms: int | None = None
    created_at: datetime


class InstalledPlugin(BaseModel):
    """Record of an installed plugin."""

    id: str  # pack name
    version: str
    source_url: str
    source_ref: str  # commit SHA
    installed_at: datetime
    enabled: bool = True
    manifest: dict[str, Any] = Field(default_factory=dict)
    permissions_granted: dict[str, Any] = Field(default_factory=dict)
