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
