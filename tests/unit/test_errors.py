"""Tests for error hierarchy."""

from __future__ import annotations

from research_engine.domain.errors import (
    ConfigurationError,
    DispatchMiss,
    EvidenceNotFound,
    LLMError,
    LLMProviderDown,
    LLMRateLimited,
    NotFoundError,
    PermissionDenied,
    PluginConflict,
    PluginLoadError,
    ResearchEngineError,
    UnknownType,
)


def test_hierarchy():
    assert issubclass(ConfigurationError, ResearchEngineError)
    assert issubclass(LLMError, ResearchEngineError)
    assert issubclass(LLMProviderDown, LLMError)
    assert issubclass(LLMRateLimited, LLMError)
    assert issubclass(PluginLoadError, ResearchEngineError)
    assert issubclass(PluginConflict, ResearchEngineError)


def test_dispatch_miss_message():
    e = DispatchMiss("/path/to/file.xyz")
    assert "file.xyz" in str(e)


def test_evidence_not_found():
    e = EvidenceNotFound("evidence", "passage-123", "some long span text")
    assert "evidence" in str(e)
    assert "passage-123" in str(e)


def test_permission_denied():
    e = PermissionDenied("my_plugin", "network")
    assert "my_plugin" in str(e)
    assert "network" in str(e)


def test_plugin_conflict():
    e = PluginConflict("letter_sent", "history", "other_plugin")
    assert "letter_sent" in str(e)
    assert "history" in str(e)


def test_unknown_type():
    e = UnknownType("entity_type", "mythical_creature", hint="Is the plugin enabled?")
    assert "mythical_creature" in str(e)
    assert "plugin enabled" in str(e)


def test_not_found():
    e = NotFoundError("document", "abc-123")
    assert "document" in str(e)
