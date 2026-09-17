"""Discover installed plugin distributions without importing plugin code."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from research_engine import __version__ as CORE_VERSION
from research_engine_sdk.manifest import entry_module, parse_manifest_bytes

if TYPE_CHECKING:
    from collections.abc import Iterable

    from research_engine_sdk import PluginManifest

PLUGIN_ENTRY_POINT_GROUP = "research_engine.plugins"
_TOP_LEVEL_MODULE = re.compile(r"^[A-Za-z_]\w*$")


@dataclass(frozen=True, slots=True)
class DiscoveryIssue:
    entry_point_name: str
    distribution_name: str
    reason: str

    def __str__(self) -> str:
        identity = self.distribution_name or self.entry_point_name or "unknown distribution"
        return f"{identity}: {self.reason}"


class PluginDiscoveryError(Exception):
    def __init__(self, issues: Iterable[DiscoveryIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues))


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    plugin_id: str
    module_name: str
    entry_point_name: str
    distribution_name: str
    distribution_version: str
    project_urls: dict[str, str]
    direct_url: dict[str, Any] | None
    manifest: PluginManifest
    manifest_sha256: str
    manifest_bytes: bytes
    manifest_path: Path
    package_root: Path

    def resource_path(self, relative_path: str) -> Path:
        """Resolve one already-validated manifest resource inside the package."""

        candidate = (self.package_root / relative_path).resolve()
        root = self.package_root.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"resource {relative_path!r} escapes package {self.module_name!r}")
        return candidate


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    plugins: tuple[DiscoveredPlugin, ...]
    issues: tuple[DiscoveryIssue, ...]

    def require_valid(self) -> list[DiscoveredPlugin]:
        if self.issues:
            raise PluginDiscoveryError(self.issues)
        return list(self.plugins)


def _entry_points() -> list[metadata.EntryPoint]:
    return list(metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP))


def _distribution_name(dist: metadata.Distribution | None) -> str:
    if dist is None:
        return ""
    return dist.metadata.get("Name", "").strip()


def _project_urls(dist: metadata.Distribution) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in dist.metadata.get_all("Project-URL") or []:
        label, separator, url = value.partition(",")
        if separator and label.strip() and url.strip():
            result[label.strip()] = url.strip()
    homepage = dist.metadata.get("Home-page")
    if homepage and "Homepage" not in result:
        result["Homepage"] = homepage
    return result


def _direct_url(dist: metadata.Distribution) -> dict[str, Any] | None:
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"invalid": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _dist_files(dist: metadata.Distribution) -> dict[str, Any]:
    return {
        PurePosixPath(str(file)).as_posix(): file
        for file in (dist.files or ())
    }


def _validate_compatibility(manifest: PluginManifest) -> str | None:
    if Version(CORE_VERSION) not in SpecifierSet(manifest.requires.core_api):
        return (
            f"requires core_api {manifest.requires.core_api}, "
            f"but core is {CORE_VERSION}"
        )
    python_version = Version(
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if python_version not in SpecifierSet(manifest.requires.python):
        return (
            f"requires Python {manifest.requires.python}, "
            f"but Python is {python_version}"
        )
    return None


def _discover_one(entry_point: metadata.EntryPoint) -> DiscoveredPlugin:
    dist = entry_point.dist
    distribution_name = _distribution_name(dist)
    if dist is None or not distribution_name:
        raise ValueError("entry point is not attached to a named distribution")
    try:
        distribution_version = str(Version(dist.version))
    except InvalidVersion as exc:
        raise ValueError(f"invalid distribution version {dist.version!r}") from exc

    value = entry_point.value.strip()
    if not _TOP_LEVEL_MODULE.fullmatch(value):
        raise ValueError(
            "entry-point value must be one top-level module with no attribute or extras"
        )
    if value in sys.stdlib_module_names:
        raise ValueError(f"top-level module {value!r} shadows the Python standard library")

    files = _dist_files(dist)
    direct_url = _direct_url(dist)
    manifest_member = f"{value}/plugin.yaml"
    packaged_manifest = files.get(manifest_member)
    if packaged_manifest is not None:
        manifest_path = Path(dist.locate_file(packaged_manifest)).resolve()
    elif (
        direct_url
        and (direct_url.get("dir_info") or {}).get("editable") is True
        and urlparse(str(direct_url.get("url", ""))).scheme == "file"
    ):
        source_root = Path(
            unquote(urlparse(str(direct_url["url"])).path)
        ).resolve()
        manifest_path = (source_root / value / "plugin.yaml").resolve()
        if not manifest_path.is_file():
            raise ValueError(f"editable distribution does not contain {manifest_member}")
    else:
        raise ValueError(f"distribution does not contain {manifest_member}")
    package_root = manifest_path.parent
    manifest_bytes = manifest_path.read_bytes()
    manifest = parse_manifest_bytes(manifest_bytes)

    if entry_point.name != manifest.plugin_id:
        raise ValueError(
            f"entry-point name {entry_point.name!r} does not match "
            f"plugin_id {manifest.plugin_id!r}"
        )

    package_prefix = f"{value}/"
    for entry in manifest.entry_values():
        module = entry_module(entry)
        if module != value and not module.startswith(f"{value}."):
            raise ValueError(
                f"entry {entry!r} is outside top-level package {value!r}"
            )
        module_member = module.replace(".", "/")
        relative_module = module.removeprefix(value).lstrip(".").replace(".", "/")
        local_module = package_root / relative_module if relative_module else package_root
        if (
            f"{module_member}.py" not in files
            and f"{module_member}/__init__.py" not in files
            and not local_module.with_suffix(".py").is_file()
            and not (local_module / "__init__.py").is_file()
        ):
            raise ValueError(f"entry module {module!r} is absent from distribution")
    for resource in manifest.resource_paths():
        member = f"{package_prefix}{PurePosixPath(resource).as_posix()}"
        if member not in files and not (package_root / resource).is_file():
            raise ValueError(f"declared resource {resource!r} is absent from distribution")

    incompatibility = _validate_compatibility(manifest)
    if incompatibility is not None:
        raise ValueError(incompatibility)

    return DiscoveredPlugin(
        plugin_id=manifest.plugin_id,
        module_name=value,
        entry_point_name=entry_point.name,
        distribution_name=distribution_name,
        distribution_version=distribution_version,
        project_urls=_project_urls(dist),
        direct_url=direct_url,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_bytes=manifest_bytes,
        manifest_path=manifest_path,
        package_root=package_root,
    )


def scan_plugins(
    entry_points: Iterable[metadata.EntryPoint] | None = None,
) -> DiscoveryReport:
    """Return all valid discoveries plus deterministic rejection reasons."""

    candidates = sorted(
        list(entry_points) if entry_points is not None else _entry_points(),
        key=lambda ep: (
            canonicalize_name(_distribution_name(ep.dist)),
            ep.name,
            ep.value,
        ),
    )
    issues: list[DiscoveryIssue] = []
    rejected_entries: set[int] = set()

    by_distribution: dict[str, list[tuple[int, metadata.EntryPoint]]] = {}
    for index, entry_point in enumerate(candidates):
        name = canonicalize_name(_distribution_name(entry_point.dist))
        by_distribution.setdefault(name, []).append((index, entry_point))
    for name, rows in by_distribution.items():
        if name and len(rows) > 1:
            reason = "distribution declares more than one research_engine.plugins entry point"
            for index, entry_point in rows:
                rejected_entries.add(index)
                issues.append(
                    DiscoveryIssue(entry_point.name, _distribution_name(entry_point.dist), reason)
                )

    discovered: list[DiscoveredPlugin] = []
    for index, entry_point in enumerate(candidates):
        if index in rejected_entries:
            continue
        try:
            discovered.append(_discover_one(entry_point))
        except Exception as exc:
            issues.append(
                DiscoveryIssue(
                    entry_point.name,
                    _distribution_name(entry_point.dist),
                    str(exc) or type(exc).__name__,
                )
            )

    duplicated_ids = {
        plugin_id
        for plugin_id in {plugin.plugin_id for plugin in discovered}
        if sum(plugin.plugin_id == plugin_id for plugin in discovered) > 1
    }
    duplicated_modules = {
        module
        for module in {plugin.module_name for plugin in discovered}
        if sum(plugin.module_name == module for plugin in discovered) > 1
    }
    accepted: list[DiscoveredPlugin] = []
    for plugin in discovered:
        reasons: list[str] = []
        if plugin.plugin_id in duplicated_ids:
            reasons.append(f"duplicate plugin_id {plugin.plugin_id!r}")
        if plugin.module_name in duplicated_modules:
            reasons.append(f"duplicate top-level module {plugin.module_name!r}")
        if reasons:
            issues.append(
                DiscoveryIssue(
                    plugin.entry_point_name,
                    plugin.distribution_name,
                    ", ".join(reasons),
                )
            )
        else:
            accepted.append(plugin)

    return DiscoveryReport(
        plugins=tuple(sorted(accepted, key=lambda plugin: plugin.plugin_id)),
        issues=tuple(
            sorted(
                issues,
                key=lambda issue: (
                    canonicalize_name(issue.distribution_name),
                    issue.entry_point_name,
                    issue.reason,
                ),
            )
        ),
    )


def discover_plugins(
    entry_points: Iterable[metadata.EntryPoint] | None = None,
) -> list[DiscoveredPlugin]:
    """Discover installed plugins or raise with every deterministic rejection."""

    return scan_plugins(entry_points).require_valid()
