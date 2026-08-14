"""Tests for configuration."""

from __future__ import annotations

from research_engine.config.settings import Settings, load_settings


def test_default_settings():
    s = Settings()
    assert s.llm_provider == "anthropic"
    assert s.embedding_provider == "local_bge"
    assert s.ingest_concurrency == 4
    assert s.log_level == "INFO"


def test_resolved_plugins_dir():
    s = Settings()
    assert str(s.resolved_plugins_dir).endswith("plugins")


def test_load_settings():
    s = load_settings(log_level="DEBUG")
    assert s.log_level == "DEBUG"
