"""Invariants of the MCP tool surface.

Each test corresponds to a finding in the 2026-09-09 architecture overview and
exists so that a fixed inconsistency cannot silently return.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from research_engine.mcp.dispatch import CORE_TOOL_MODULES

TOOLS_DIR = pathlib.Path(inspect.getfile(CORE_TOOL_MODULES[0])).parent

# `list_filters.py` exports `list_available_filters`. Recorded as an accepted
# exception so that a second one has to be argued for, not merely typed.
STEM_EXCEPTIONS = {"list_filters": "list_available_filters"}


def _codes(module) -> set[str]:
    """Every literal error code emitted by a tool module."""
    tree = ast.parse(pathlib.Path(inspect.getfile(module)).read_text())
    return {
        v.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Dict)
        for k, v in zip(n.keys, n.values)
        if getattr(k, "value", None) == "code" and isinstance(v, ast.Constant)
    }


def test_every_module_is_registered_exactly_once():
    on_disk = {p.stem for p in TOOLS_DIR.glob("*.py") if p.stem != "__init__"}
    registered = [m.__name__.rsplit(".", 1)[-1] for m in CORE_TOOL_MODULES]
    assert on_disk == set(registered)
    assert len(registered) == len(set(registered))
    assert len({m.TOOL_NAME for m in CORE_TOOL_MODULES}) == len(registered)


def test_module_stem_matches_tool_name():
    drift = {
        m.__name__.rsplit(".", 1)[-1]: m.TOOL_NAME
        for m in CORE_TOOL_MODULES
        if m.__name__.rsplit(".", 1)[-1] != m.TOOL_NAME
    }
    assert drift == STEM_EXCEPTIONS


@pytest.mark.parametrize("module", CORE_TOOL_MODULES, ids=lambda m: m.TOOL_NAME)
def test_module_contract(module):
    assert module.TOOL_NAME and isinstance(module.TOOL_NAME, str)
    assert module.TOOL_SCHEMA.get("type") == "object"
    assert inspect.iscoroutinefunction(module.handler)
    # Descriptions are written at the agent; a one-liner is a review smell.
    assert len(module.TOOL_DESCRIPTION) >= 60, "description too thin to guide an agent"


@pytest.mark.xfail(strict=True, reason="WI-1")
def test_self_caught_failure_code_matches_tool_name():  # WI-1
    """Scans for hand-written `"code": "..._failed"` literals.

    Once WI-1 routes every catch-all through `mcp.errors.failed()`, there are no
    such literals left and this passes by having nothing to check. That is the
    intended end state — keep the test, because its job is to fail the moment
    someone reintroduces a hand-typed code.

    Aggregated over modules rather than parametrized per module: a strict xfail
    on a 39-way parametrize would XPASS-strict (i.e. fail) for the 37 modules
    that already agree with their names, so the suite could never start green.
    The offenders are listed in the assertion message instead.
    """
    drift = {
        module.TOOL_NAME: sorted(
            c for c in _codes(module) if c.endswith("_failed")
        )
        for module in CORE_TOOL_MODULES
    }
    drift = {k: v for k, v in drift.items() if v and f"{k}_failed" not in v}
    assert not drift, f"hand-typed _failed codes disagree with tool names: {drift}"


@pytest.mark.xfail(strict=True, reason="WI-3")
def test_works_guard_only_where_the_dependency_is_optional():  # WI-3
    guarded = {
        p.stem for p in TOOLS_DIR.glob("work_*.py")
        if "works_not_configured" in p.read_text()
    }
    assert guarded == {"work_citations", "work_render", "work_verify"}


@pytest.mark.xfail(strict=True, reason="WI-7")
def test_no_raw_engine_access_from_the_mcp_layer():  # WI-7
    offenders = [
        p.name for p in TOOLS_DIR.glob("*.py")
        if "container.engine" in p.read_text()
        or 'getattr(container, "engine"' in p.read_text()
    ]
    assert offenders == []
