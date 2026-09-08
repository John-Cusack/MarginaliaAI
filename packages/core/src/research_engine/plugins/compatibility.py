"""Plugin compatibility checks."""

from __future__ import annotations

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from research_engine import __version__ as CORE_VERSION


def check_core_api(core_api_spec: str, core_version: str = CORE_VERSION) -> str | None:
    """Return None if the core version satisfies the spec, else a reason string.

    Treats an unparseable specifier as incompatible (safer than crashing).
    """
    try:
        spec = SpecifierSet(core_api_spec)
    except InvalidSpecifier:
        return f"invalid core_api specifier {core_api_spec!r}"
    try:
        if Version(core_version) not in spec:
            return f"requires core_api {core_api_spec}, but core is {core_version}"
    except InvalidVersion:
        return f"cannot parse core version {core_version!r}"
    return None


def check_plugin_deps(
    required: list[tuple[str, str]], available: dict[str, str]
) -> list[str]:
    """Return one reason per declared pack dependency that is not satisfied.

    ``required`` is ``(name, version_spec)`` from a manifest's
    ``requires.plugins``; ``available`` maps the name of every pack that will
    load to its version. Empty means every dependency is present and in range.

    A pack that contributes a chunker, a filter extension or an ingestion
    module to another pack is a real dependency — without it the dependent pack
    fails at *use* time, deep inside a pipeline, with ``Unknown chunker:
    verse_boundary`` and nothing pointing at the missing install. Checking here
    turns that into a refusal to load, naming both packs.

    As with ``check_core_api``, an unparseable specifier is a failure rather
    than a crash: a manifest that cannot be understood is not one to trust.
    """
    reasons = []
    for name, spec_text in required:
        installed = available.get(name)
        if installed is None:
            reasons.append(f"requires plugin {name!r} ({spec_text}), which is not loaded")
            continue
        try:
            spec = SpecifierSet(spec_text)
        except InvalidSpecifier:
            reasons.append(f"invalid version specifier {spec_text!r} for plugin {name!r}")
            continue
        try:
            if Version(installed) not in spec:
                reasons.append(
                    f"requires plugin {name!r} {spec_text}, but {installed} is installed"
                )
        except InvalidVersion:
            reasons.append(f"cannot parse version {installed!r} of plugin {name!r}")
    return reasons
