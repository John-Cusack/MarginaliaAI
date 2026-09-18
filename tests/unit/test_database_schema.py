"""Runtime must never run against an unapplied database schema."""

from __future__ import annotations

import pytest

from research_engine.adapters.storage.postgres.migrations import runner
from research_engine.domain.errors import ConfigurationError

pytestmark = pytest.mark.unit


async def test_outdated_schema_refuses_with_the_public_upgrade_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stale(_engine: object) -> tuple[str, ...]:
        return ("018_anchor_editions",)

    monkeypatch.setattr(runner, "current_heads", stale)
    monkeypatch.setattr(runner, "expected_heads", lambda: ("019_plugin_activations",))

    with pytest.raises(ConfigurationError) as caught:
        await runner.require_current_schema(object())  # type: ignore[arg-type]

    message = str(caught.value)
    assert "018_anchor_editions" in message
    assert "019_plugin_activations" in message
    assert "research-engine db upgrade" in message


async def test_current_schema_allows_runtime_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def current(_engine: object) -> tuple[str, ...]:
        return ("head",)

    monkeypatch.setattr(runner, "current_heads", current)
    monkeypatch.setattr(runner, "expected_heads", lambda: ("head",))

    await runner.require_current_schema(object())  # type: ignore[arg-type]
