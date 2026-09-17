from __future__ import annotations

import pytest
from pydantic import ValidationError

from research_engine_sdk import PluginManifest

VALID = {
    "schema_version": 2,
    "plugin_id": "sample",
    "requires": {"core_api": ">=0.6,<0.7", "python": ">=3.11"},
    "permissions": {"network": "none", "filesystem": "plugin_data"},
    "provides": {
        "extraction_schemas": [
            {"id": "claims", "version": 1, "file": "schemas/claims.yaml"}
        ],
        "mcp_tools": [
            {
                "id": "sample.search",
                "entry": "sample.tools:search",
                "description": "Search sample data",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    },
}


def test_manifest_v2_accepts_static_runtime_contract() -> None:
    manifest = PluginManifest.model_validate(VALID)

    assert manifest.plugin_id == "sample"
    assert manifest.resource_paths() == ("schemas/claims.yaml",)
    assert manifest.entry_values() == ("sample.tools:search",)


@pytest.mark.parametrize("field", ["name", "version", "author", "license", "homepage"])
def test_manifest_v2_rejects_distribution_metadata(field: str) -> None:
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({**VALID, field: "legacy"})


@pytest.mark.parametrize("field", ["pip", "setup_commands"])
def test_manifest_v2_rejects_executable_dependency_fields(field: str) -> None:
    data = {**VALID, "requires": {**VALID["requires"], field: ["bad"]}}
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(data)


@pytest.mark.parametrize("path", ["../secret.yaml", "/tmp/secret.yaml", "schemas/../secret.yaml"])
def test_resource_paths_cannot_escape_package(path: str) -> None:
    data = {
        **VALID,
        "provides": {
            **VALID["provides"],
            "extraction_schemas": [{"id": "bad", "version": 1, "file": path}],
        },
    }
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(data)
