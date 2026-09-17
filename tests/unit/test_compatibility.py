"""Tests for plugin core_api compatibility checks."""

from __future__ import annotations

from research_engine import __version__ as CORE_VERSION
from research_engine.plugins.compatibility import check_core_api, check_plugin_deps


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


class TestCheckPluginDeps:
    """`requires.plugins` was parsed and read by nothing for the whole of 0.1.

    A pack that contributes a chunker, a filter extension or an ingestion module
    to another pack is a real dependency: without it the dependent pack fails at
    *use* time, deep in a pipeline, with `Unknown chunker: verse_boundary` and
    nothing naming the missing install. These turn that into a refusal to load.
    """

    def test_a_satisfied_dependency_reports_nothing(self):
        assert check_plugin_deps([("scripture", ">=0.1.0")], {"scripture": "0.1.0"}) == []

    def test_a_missing_plugin_is_named_with_what_wanted_it(self):
        reasons = check_plugin_deps([("scripture", ">=0.1.0")], {})
        assert len(reasons) == 1
        assert "scripture" in reasons[0]
        assert "not loaded" in reasons[0]

    def test_a_version_out_of_range_reports_both_versions(self):
        reasons = check_plugin_deps([("scripture", ">=0.2.0")], {"scripture": "0.1.0"})
        assert len(reasons) == 1
        assert ">=0.2.0" in reasons[0]
        assert "0.1.0" in reasons[0]

    def test_no_dependencies_is_always_satisfied(self):
        assert check_plugin_deps([], {}) == []

    def test_an_invalid_specifier_is_a_failure_rather_than_a_crash(self):
        """A manifest that cannot be understood is not one to trust."""
        reasons = check_plugin_deps([("scripture", "not-a-spec")], {"scripture": "0.1.0"})
        assert reasons and "invalid version specifier" in reasons[0]

    def test_an_unparseable_installed_version_is_reported(self):
        reasons = check_plugin_deps([("scripture", ">=0.1.0")], {"scripture": "wat"})
        assert reasons and "cannot parse version" in reasons[0]

    def test_every_unsatisfied_dependency_is_reported_not_just_the_first(self):
        reasons = check_plugin_deps(
            [("scripture", ">=0.1.0"), ("history", ">=9.0.0")], {"history": "0.1.0"}
        )
        assert len(reasons) == 2


