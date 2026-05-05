"""Permission gates and denied clients for plugin sandboxing."""

from __future__ import annotations

from research_engine.domain.errors import PermissionDenied
from research_engine.plugins.manifest import NetworkPerm, PluginPermissions


def check_network(permissions: PluginPermissions, url: str, plugin_name: str) -> None:
    """Check if a plugin is allowed to make a network request."""
    if permissions.network == NetworkPerm.none:
        raise PermissionDenied(plugin_name, "network")

    if permissions.network == NetworkPerm.egress and permissions.network_allowlist:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if not any(host.endswith(domain) for domain in permissions.network_allowlist):
            raise PermissionDenied(
                plugin_name,
                f"network access to {host} (not in allowlist: {permissions.network_allowlist})",
            )


def check_llm(permissions: PluginPermissions, plugin_name: str) -> None:
    """Check if a plugin is allowed to use LLM."""
    if not permissions.llm:
        raise PermissionDenied(plugin_name, "llm")


def check_subprocess(permissions: PluginPermissions, plugin_name: str) -> None:
    """Check if a plugin is allowed to spawn subprocesses."""
    if not permissions.subprocess:
        raise PermissionDenied(plugin_name, "subprocess")


class DeniedLLMClient:
    """LLM client that always denies access."""

    def __init__(self, plugin_name: str) -> None:
        self._plugin = plugin_name

    async def complete(self, *args, **kwargs):
        raise PermissionDenied(self._plugin, "llm")

    async def structured(self, *args, **kwargs):
        raise PermissionDenied(self._plugin, "llm")


class DeniedHttpClient:
    """HTTP client that always denies access."""

    def __init__(self, plugin_name: str) -> None:
        self._plugin = plugin_name

    async def get(self, *args, **kwargs):
        raise PermissionDenied(self._plugin, "network")

    async def post(self, *args, **kwargs):
        raise PermissionDenied(self._plugin, "network")

    async def close(self) -> None:
        pass


class DeniedIngestionClient:
    """Ingestion client that always denies access."""

    def __init__(self, plugin_name: str) -> None:
        self._plugin = plugin_name

    async def ingest_paths(self, *args, **kwargs):
        raise PermissionDenied(self._plugin, "ingest")

    async def ingest_drafts(self, *args, **kwargs):
        raise PermissionDenied(self._plugin, "ingest")

    async def find_existing(self, *args, **kwargs):
        raise PermissionDenied(self._plugin, "ingest")


class GatedHttpClient:
    """HTTP client that filters requests against an allowlist."""

    def __init__(self, inner, permissions: PluginPermissions, plugin_name: str) -> None:
        self._inner = inner
        self._permissions = permissions
        self._plugin = plugin_name

    async def get(self, url: str, **kwargs):
        check_network(self._permissions, url, self._plugin)
        return await self._inner.get(url, **kwargs)

    async def post(self, url: str, **kwargs):
        check_network(self._permissions, url, self._plugin)
        return await self._inner.post(url, **kwargs)

    async def close(self) -> None:
        pass
