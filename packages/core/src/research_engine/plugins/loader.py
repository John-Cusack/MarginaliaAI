"""Plugin loader — 8-phase loading pipeline."""

from __future__ import annotations

import importlib
import re
import sys
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

import structlog

from research_engine.domain.errors import PluginLoadError
from research_engine.plugins.manifest import PluginManifest, parse_manifest
from research_engine.plugins.permissions import (
    DeniedHttpClient,
    DeniedIngestionClient,
    DeniedLLMClient,
    GatedHttpClient,
)

if TYPE_CHECKING:
    from pathlib import Path

    from research_engine.plugins.registry import PluginRegistry
    from research_engine.ports.repositories import InstalledPluginRepo

logger = structlog.get_logger()


class LoadedPlugin:
    def __init__(self, manifest: PluginManifest, plugin_dir: Path) -> None:
        self.manifest = manifest
        self.plugin_dir = plugin_dir
        self.tools: dict[str, Any] = {}


class PluginLoader:
    def __init__(
        self,
        installed_plugins: InstalledPluginRepo,
        registry: PluginRegistry,
        plugins_dir: Path,
        llm: Any = None,
        http: Any = None,
        **services: Any,
    ) -> None:
        self._installed = installed_plugins
        self._registry = registry
        self._plugins_dir = plugins_dir
        self._llm = llm
        self._http = http
        self._services = services
        self._loaded: dict[str, LoadedPlugin] = {}

    # Packages where the pip name differs from the import name.
    _IMPORT_NAME_OVERRIDES: dict[str, str] = {
        "pillow": "PIL",
        "beautifulsoup4": "bs4",
        "scikit-learn": "sklearn",
        "python-dateutil": "dateutil",
        "python-dotenv": "dotenv",
        "pyyaml": "yaml",
    }

    @staticmethod
    def _check_pip_deps(pip_deps: list[str]) -> list[str]:
        """Check which pip dependencies are missing. Returns list of missing package names."""
        # Pattern to extract package name from PEP 508 strings like "playwright>=1.40"
        _pkg_name_re = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)")
        missing = []
        for dep in pip_deps:
            match = _pkg_name_re.match(dep)
            if not match:
                continue
            pkg_name = match.group(1)
            pkg_lower = pkg_name.lower()
            # Check override table first, then normalize
            if pkg_lower in PluginLoader._IMPORT_NAME_OVERRIDES:
                import_name = PluginLoader._IMPORT_NAME_OVERRIDES[pkg_lower]
            else:
                import_name = pkg_name.replace("-", "_").replace(".", "_")
            if find_spec(import_name) is None:
                missing.append(pkg_name)
        return missing

    async def load_enabled(self) -> list[str]:
        """Load all enabled plugins. Returns list of loaded plugin names."""
        enabled = await self._installed.list_enabled()
        loaded_names = []

        # Phase 1: Discover
        plugins_to_load: list[tuple[Any, Path]] = []
        for installed in enabled:
            plugin_dir = self._plugins_dir / f"{installed.id}@{installed.version}"
            manifest_path = plugin_dir / "pack.yaml"
            if not manifest_path.exists():
                logger.warning("plugin_manifest_missing", plugin=installed.id, dir=str(plugin_dir))
                continue
            plugins_to_load.append((installed, plugin_dir))

        # Phase 2-3: Validate manifest and dependencies
        validated: list[tuple[Any, Path, PluginManifest]] = []
        for installed, plugin_dir in plugins_to_load:
            try:
                manifest = parse_manifest(plugin_dir / "pack.yaml")

                # Check pip dependencies are importable
                missing = self._check_pip_deps(manifest.requires.pip)
                if missing:
                    logger.error(
                        "plugin_missing_pip_deps",
                        plugin=installed.id,
                        missing=missing,
                    )
                    continue

                validated.append((installed, plugin_dir, manifest))
            except Exception as e:
                logger.error("plugin_validate_failed", plugin=installed.id, error=str(e))

        # Phase 4-8: Register types, load code, register contributions
        for _installed, plugin_dir, manifest in validated:
            try:
                await self._load_one(manifest, plugin_dir)
                loaded_names.append(manifest.name)
                logger.info("plugin_loaded", plugin=manifest.name, version=manifest.version)
            except Exception as e:
                logger.error("plugin_load_failed", plugin=manifest.name, error=str(e))

        return loaded_names

    async def _load_one(self, manifest: PluginManifest, plugin_dir: Path) -> None:
        loaded = LoadedPlugin(manifest, plugin_dir)
        provides = manifest.provides

        # Phase 4: Register types
        for dt in provides.document_types:
            self._registry.register_document_type(
                dt.id, {"default_chunker": dt.default_chunker}, manifest.name
            )
        for et in provides.entity_types:
            self._registry.register_entity_type(et.id, {}, manifest.name)
        for evt in provides.event_types:
            self._registry.register_event_type(evt.id, {}, manifest.name)
        for rt in provides.relation_types:
            self._registry.register_relation_type(
                rt.id, {"inverse": rt.inverse}, manifest.name
            )

        # Phase 4b: Register chunkers
        for chunker_contrib in provides.chunkers:
            try:
                chunker_cls = self._import_entry(plugin_dir, chunker_contrib.entry)
                self._registry.register_chunker(chunker_contrib.id, chunker_cls, manifest.name)
            except Exception as e:
                raise PluginLoadError(
                    f"Failed to load chunker {chunker_contrib.id}: {e}"
                ) from e

        # Phase 4c: Register ingestion modules
        for im_contrib in provides.ingestion_modules:
            try:
                im_cls = self._import_entry(plugin_dir, im_contrib.entry)
                self._registry.register_ingestion_module(im_contrib.id, im_cls, manifest.name)
            except Exception as e:
                raise PluginLoadError(
                    f"Failed to load ingestion module {im_contrib.id}: {e}"
                ) from e

        # Phase 4d: Register filter extensions
        for fe_contrib in provides.filter_extensions:
            try:
                fe_cls = self._import_entry(plugin_dir, fe_contrib.entry)
                instance = fe_cls() if isinstance(fe_cls, type) else fe_cls
                self._registry.register_filter_extension(fe_contrib.id, instance, manifest.name)
            except Exception as e:
                raise PluginLoadError(
                    f"Failed to load filter extension {fe_contrib.id}: {e}"
                ) from e

        # Phase 4e: Register source search providers
        for ss_contrib in provides.source_search:
            try:
                ss_cls = self._import_entry(plugin_dir, ss_contrib.entry)
                provider = ss_cls() if isinstance(ss_cls, type) else ss_cls
                self._registry.register_source_search_provider(provider, manifest.name)
            except Exception as e:
                raise PluginLoadError(
                    f"Failed to load source search provider {ss_contrib.id}: {e}"
                ) from e

        # Phase 5: Load code
        for tool_contrib in provides.mcp_tools:
            try:
                handler = self._import_entry(plugin_dir, tool_contrib.entry)
                loaded.tools[tool_contrib.id] = handler
            except Exception as e:
                raise PluginLoadError(
                    f"Failed to load tool {tool_contrib.id}: {e}"
                ) from e

        # Phase 6: Register contributions
        for tool_id, handler in loaded.tools.items():
            self._registry.register_mcp_tool(tool_id, handler, manifest.name)

        # Phase 7: Extraction schemas
        for es in provides.extraction_schemas:
            schema_path = plugin_dir / es.file
            if schema_path.exists():
                import yaml
                with open(schema_path) as f:
                    schema_data = yaml.safe_load(f)
                self._registry.register_extraction_schema(
                    es.id, es.version, schema_data, manifest.name
                )

        self._loaded[manifest.name] = loaded

    def _import_entry(self, plugin_dir: Path, entry: str) -> Any:
        """Import a plugin entry point like 'module.path:attr'."""
        module_path, attr = entry.rsplit(":", 1)
        old_path = sys.path[:]
        sys.path.insert(0, str(plugin_dir))
        try:
            mod = importlib.import_module(module_path)
            return getattr(mod, attr)
        finally:
            sys.path[:] = old_path

    def build_plugin_clients(self, plugin_name: str) -> dict[str, Any]:
        """Build scoped clients for a plugin."""
        loaded = self._loaded.get(plugin_name)
        if not loaded:
            return {}

        perms = loaded.manifest.permissions

        # Corpus client: adapter that conforms search + docs + passages to the
        # SDK CorpusClient Protocol. Built per-call so it shares no state across
        # plugin invocations. Falls back to the raw search service if the
        # repos weren't passed in (older composition wiring).
        from research_engine.adapters.corpus_client import CorpusServiceAdapter

        search = self._services.get("search")
        documents = self._services.get("documents")
        passages = self._services.get("passages")
        if search is not None and documents is not None and passages is not None:
            corpus_client: Any = CorpusServiceAdapter(search, documents, passages)
        else:
            corpus_client = search

        clients = {
            "corpus": corpus_client,
            "entity": self._services.get("entity_service"),
            "event": self._services.get("event_service"),
            "extraction": self._services.get("extraction"),
        }

        # LLM client
        if perms.llm and self._llm:
            clients["llm"] = self._llm
        else:
            clients["llm"] = DeniedLLMClient(plugin_name)

        # HTTP client
        if perms.network.value != "none" and self._http:
            clients["http"] = GatedHttpClient(self._http, perms, plugin_name)
        else:
            clients["http"] = DeniedHttpClient(plugin_name)

        # Ingestion client
        if perms.ingest and self._services.get("ingestion"):
            clients["ingestion"] = self._services["ingestion"]
        else:
            clients["ingestion"] = DeniedIngestionClient(plugin_name)

        return clients

    @property
    def loaded_plugins(self) -> dict[str, LoadedPlugin]:
        return dict(self._loaded)
