"""Provenance and operations domain types."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

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


class PluginActivationState(enum.StrEnum):
    available = "available"
    enabled = "enabled"
    disabled = "disabled"
    pending_approval = "pending_approval"
    missing = "missing"
    incompatible = "incompatible"
    error = "error"
    legacy = "legacy"


class PluginActivation(BaseModel):
    """Approval and runtime state for one installed plugin distribution."""

    plugin_id: str
    distribution_name: str | None = None
    distribution_version: str
    entry_point_name: str | None = None
    manifest_sha256: str | None = None
    manifest: dict[str, Any]
    permissions_granted: dict[str, Any]
    installed_at: datetime
    enabled: bool = False
    state: PluginActivationState = PluginActivationState.legacy
    approved_at: datetime | None = None
    approved_non_interactive: bool = False
    last_seen_at: datetime | None = None
    last_error: str | None = None
    provenance: dict[str, Any] | None = None
    legacy_source_url: str | None = None
    legacy_source_ref: str | None = None
    database_revision: int | None = None
    database_status: str | None = None

    @model_validator(mode="after")
    def _approved_rows_have_distribution_identity(self) -> PluginActivation:
        if self.state is PluginActivationState.legacy:
            return self
        required = {
            "distribution_name": self.distribution_name,
            "entry_point_name": self.entry_point_name,
            "manifest_sha256": self.manifest_sha256,
            "approved_at": self.approved_at,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                "non-legacy plugin activation requires " + ", ".join(missing)
            )
        return self
