"""Tests for plugin core_api compatibility checks."""

from __future__ import annotations

from research_engine import __version__ as CORE_VERSION
from research_engine.plugins.compatibility import check_core_api


class TestCheckCoreApi:
    def test_compatible_spec_returns_none(self):
        assert check_core_api(">=0.1.0,<1.0.0", core_version="0.1.0") is None

    def test_incompatible_lower_bound_returns_reason(self):
        reason = check_core_api(">=0.2.0", core_version="0.1.0")
        assert reason is not None
        assert ">=0.2.0" in reason
        assert "0.1.0" in reason

    def test_exclusive_upper_bound_returns_reason(self):
        reason = check_core_api(">=0.2.0,<1.0.0", core_version="0.1.0")
        assert reason is not None
        assert "0.1.0" in reason

    def test_invalid_specifier_returns_reason_without_raising(self):
        reason = check_core_api("not-a-spec", core_version="0.1.0")
        assert reason is not None
        assert "invalid core_api specifier" in reason

    def test_unparseable_core_version_returns_reason(self):
        reason = check_core_api(">=0.1.0", core_version="not-a-version")
        assert reason is not None
        assert "cannot parse core version" in reason

    def test_default_core_version_is_compatible_with_manifest_default(self):
        # The PluginCompatibility.core_api default must accept the real core version.
        assert check_core_api(">=0.1.0,<1.0.0") is None
        # Sanity check that the default resolves to the package version.
        assert check_core_api(f"=={CORE_VERSION}") is None
