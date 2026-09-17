from __future__ import annotations

import sys
from importlib import metadata
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from research_engine.domain.errors import PluginLoadError
from research_engine.plugins.discovery import discover_plugins, scan_plugins
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.registry import PluginRegistry


def _fixture_distribution(
    tmp_path,
    *,
    plugin_id: str = "sample",
    module_name: str | None = None,
    distribution_name: str | None = None,
    version: str = "1.0.0",
    core_api: str = ">=0.6,<0.7",
    entry_value: str | None = None,
    entry_name: str | None = None,
    raise_on_import: bool = False,
    tool_has_handler: bool = True,
    include_tool_schema: bool = True,
    resource_path: str = "schemas/claims.yaml",
    resource_present: bool = True,
):
    module_name = module_name or plugin_id.replace("-", "_")
    distribution_name = distribution_name or f"marginalia-ai-plugin-{plugin_id}"
    site = tmp_path / f"site-{module_name}"
    package = site / module_name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "raise RuntimeError('plugin imported during discovery')\n"
        if raise_on_import
        else ""
    )
    (package / "tools.py").write_text(
        "async def handler(**kwargs):\n    return {'plugin': __package__}\n"
        if tool_has_handler
        else "VALUE = 1\n"
    )
    resource = package / resource_path
    if resource_present and ".." not in resource_path:
        resource.parent.mkdir(parents=True, exist_ok=True)
        resource.write_text("id: claims\nrecord_types: []\n")

    tool = {
        "id": f"{plugin_id}.run",
        "entry": f"{module_name}.tools:handler",
        "description": f"Run {plugin_id}",
    }
    if include_tool_schema:
        tool["input_schema"] = {"type": "object", "properties": {}}
    manifest = {
        "schema_version": 2,
        "plugin_id": plugin_id,
        "requires": {"core_api": core_api, "python": ">=3.11"},
        "permissions": {"network": "none", "filesystem": "plugin_data"},
        "provides": {
            "document_types": [
                {
                    "id": f"{plugin_id.replace('-', '_')}_document",
                    "default_chunker": "whole_or_paragraph",
                }
            ],
            "extraction_schemas": [
                {"id": f"{plugin_id}_claims", "version": 1, "file": resource_path}
            ],
            "vocabularies": [
                {"id": f"{plugin_id}_vocabulary", "file": resource_path}
            ],
            "mcp_tools": [tool],
        },
    }
    (package / "plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    normalized = distribution_name.replace("-", "_")
    dist_info = site / f"{normalized}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.4\n"
        f"Name: {distribution_name}\n"
        f"Version: {version}\n"
        "Project-URL: Source, https://example.test/source\n"
    )
    (dist_info / "entry_points.txt").write_text(
        "[research_engine.plugins]\n"
        f"{entry_name or plugin_id} = {entry_value or module_name}\n"
    )
    files = [path.relative_to(site).as_posix() for path in site.rglob("*") if path.is_file()]
    record = dist_info / "RECORD"
    files.append(record.relative_to(site).as_posix())
    record.write_text("".join(f"{path},,\n" for path in files))

    distribution = metadata.PathDistribution(dist_info)
    [entry_point] = distribution.entry_points
    return entry_point, site


def test_discovery_reads_manifest_without_importing_package(tmp_path) -> None:
    entry_point, _ = _fixture_distribution(tmp_path, raise_on_import=True)
    sys.modules.pop("sample", None)

    [plugin] = discover_plugins([entry_point])

    assert plugin.plugin_id == "sample"
    assert plugin.distribution_name == "marginalia-ai-plugin-sample"
    assert plugin.project_urls["Source"] == "https://example.test/source"
    assert len(plugin.manifest_sha256) == 64
    assert "sample" not in sys.modules


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"entry_name": "different"}, "does not match plugin_id"),
        ({"entry_value": "sample.tools:handler"}, "one top-level module"),
        ({"resource_present": False}, "absent from distribution"),
        ({"resource_path": "../claims.yaml"}, "traverse"),
        ({"core_api": ">=99"}, "requires core_api"),
    ],
)
def test_invalid_distribution_has_deterministic_reason(
    tmp_path, overrides: dict, reason: str
) -> None:
    entry_point, _ = _fixture_distribution(tmp_path, **overrides)

    report = scan_plugins([entry_point])

    assert report.plugins == ()
    assert len(report.issues) == 1
    assert reason in report.issues[0].reason


def test_stdlib_package_name_is_rejected(tmp_path) -> None:
    entry_point, _ = _fixture_distribution(
        tmp_path,
        plugin_id="stdlib-plugin",
        module_name="json",
    )

    report = scan_plugins([entry_point])

    assert "standard library" in report.issues[0].reason


def test_duplicate_plugin_ids_reject_both_distributions(tmp_path) -> None:
    first, _ = _fixture_distribution(
        tmp_path,
        plugin_id="duplicate",
        module_name="duplicate_one",
        distribution_name="marginalia-ai-plugin-duplicate-one",
    )
    second, _ = _fixture_distribution(
        tmp_path,
        plugin_id="duplicate",
        module_name="duplicate_two",
        distribution_name="marginalia-ai-plugin-duplicate-two",
    )

    report = scan_plugins([first, second])

    assert report.plugins == ()
    assert [issue.reason for issue in report.issues] == [
        "duplicate plugin_id 'duplicate'",
        "duplicate plugin_id 'duplicate'",
    ]


def test_missing_tool_schema_is_rejected_before_import(tmp_path) -> None:
    entry_point, _ = _fixture_distribution(tmp_path, include_tool_schema=False)

    report = scan_plugins([entry_point])

    assert report.plugins == ()
    assert "input_schema" in report.issues[0].reason


async def test_failed_load_leaves_registry_unchanged(tmp_path, monkeypatch) -> None:
    entry_point, site = _fixture_distribution(
        tmp_path,
        plugin_id="broken",
        module_name="broken_plugin",
        tool_has_handler=False,
    )
    monkeypatch.syspath_prepend(str(site))
    [plugin] = discover_plugins([entry_point])
    registry = PluginRegistry()
    registry.register_core_types()
    loader = PluginLoader(AsyncMock(), registry, tmp_path / "plugin-data")

    with pytest.raises(PluginLoadError, match="does not exist"):
        await loader._load_one(plugin)

    assert registry.list_document_types() == {}
    assert registry.get_extraction_schemas() == []
    assert registry.get_vocabularies() == {}
    assert registry.get_mcp_tools() == {}


def _activation(plugin):
    return SimpleNamespace(
        plugin_id=plugin.plugin_id,
        distribution_name=plugin.distribution_name,
        distribution_version=plugin.distribution_version,
        entry_point_name=plugin.entry_point_name,
        manifest_sha256=plugin.manifest_sha256,
        enabled=True,
        state="enabled",
    )


async def test_two_valid_plugins_load_in_plugin_id_order(tmp_path, monkeypatch) -> None:
    beta_entry, beta_site = _fixture_distribution(
        tmp_path,
        plugin_id="beta",
        module_name="beta_plugin",
    )
    alpha_entry, alpha_site = _fixture_distribution(
        tmp_path,
        plugin_id="alpha",
        module_name="alpha_plugin",
    )
    monkeypatch.syspath_prepend(str(beta_site))
    monkeypatch.syspath_prepend(str(alpha_site))
    plugins = discover_plugins([beta_entry, alpha_entry])
    repository = AsyncMock()
    repository.list_enabled.return_value = [_activation(plugin) for plugin in plugins]
    registry = PluginRegistry()
    registry.register_core_types()
    loader = PluginLoader(repository, registry, tmp_path / "plugin-data")

    loaded = await loader.load_enabled(reversed(plugins))

    assert loaded == ["alpha", "beta"]
    assert set(registry.get_mcp_tools()) == {"alpha.run", "beta.run"}
    assert set(registry.list_document_types()) == {"alpha_document", "beta_document"}
