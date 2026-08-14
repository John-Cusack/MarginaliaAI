"""Tests for plugin manifest parsing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from research_engine.plugins.manifest import PluginManifest, parse_manifest

if TYPE_CHECKING:
    from pathlib import Path


class TestPluginManifest:
    def test_minimal(self):
        m = PluginManifest(
            name="test",
            version="0.1.0",
            author="Test Author",
            description="A test plugin",
        )
        assert m.name == "test"
        assert m.permissions.network.value == "none"
        assert m.permissions.llm is False

    def test_parse_yaml(self, tmp_path: Path):
        yaml_content = """
name: history
version: 0.1.0
author: MarginaliaAI
description: History research pack
license: Apache-2.0

requires:
  core_api: ">=0.1.0,<1.0.0"

permissions:
  network: none
  llm: true

provides:
  document_types:
    - id: letter
      default_chunker: whole_or_paragraph
  entity_types:
    - id: person
  event_types:
    - id: letter_sent
"""
        manifest_path = tmp_path / "pack.yaml"
        manifest_path.write_text(yaml_content)

        m = parse_manifest(manifest_path)
        assert m.name == "history"
        assert m.permissions.llm is True
        assert len(m.provides.document_types) == 1
        assert m.provides.document_types[0].id == "letter"
        assert len(m.provides.entity_types) == 1
        assert len(m.provides.event_types) == 1
