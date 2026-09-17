from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_engine.domain.provenance import (
    PluginActivation,
    PluginActivationState,
)


def test_legacy_activation_preserves_nullable_distribution_identity() -> None:
    activation = PluginActivation(
        plugin_id="history",
        distribution_version="0.1.0",
        manifest={"name": "history"},
        permissions_granted={"llm": True},
        installed_at=datetime.now(UTC),
        state=PluginActivationState.legacy,
        legacy_source_url="https://example.test/history.git",
        legacy_source_ref="abc123",
    )

    assert activation.enabled is False
    assert activation.distribution_name is None


def test_nonlegacy_activation_requires_exact_approval_identity() -> None:
    with pytest.raises(ValidationError, match="distribution_name"):
        PluginActivation(
            plugin_id="history",
            distribution_version="0.2.0",
            manifest={"schema_version": 2, "plugin_id": "history"},
            permissions_granted={"llm": True},
            installed_at=datetime.now(UTC),
            state=PluginActivationState.enabled,
            enabled=True,
        )


def test_approved_activation_records_exact_artifact() -> None:
    approved_at = datetime.now(UTC)
    activation = PluginActivation(
        plugin_id="history",
        distribution_name="marginalia-ai-plugin-history",
        distribution_version="0.2.0",
        entry_point_name="history",
        manifest_sha256="a" * 64,
        manifest={"schema_version": 2, "plugin_id": "history"},
        permissions_granted={"llm": True},
        installed_at=approved_at,
        approved_at=approved_at,
        state=PluginActivationState.enabled,
        enabled=True,
    )

    assert activation.manifest_sha256 == "a" * 64
    assert activation.approved_at == approved_at
