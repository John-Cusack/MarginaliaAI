"""Every command group must actually build.

Typer resolves a command's type annotations at runtime to construct its
options, so an import that exists only for type checkers breaks the command at
startup — not at type-check time, and not in any test that imports the service
layer directly.

That is exactly how it broke: a lint autofix moved `from pathlib import Path`
into a `TYPE_CHECKING` block in two CLI modules. Unit tests stayed green, ruff
was clean, and `research-engine` could not start at all — it died with
`NameError: name 'Path' is not defined` before running anything.

These tests are deliberately shallow. They assert only that each command group
can be constructed and print its help, which is the precise property that was
silently lost.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from research_engine.cli.main import app

runner = CliRunner()

COMMAND_GROUPS = [
    "backup",
    "config",
    "doctor",
    "embeddings",
    "ingest",
    "plugin",
    "reindex",
    "search",
    "status",
]


def test_the_root_command_builds() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("group", COMMAND_GROUPS)
def test_each_command_group_builds(group: str) -> None:
    """A group that cannot resolve its annotations fails here, not in production."""
    result = runner.invoke(app, [group, "--help"])
    assert result.exit_code == 0, (
        f"`research-engine {group} --help` exited {result.exit_code}.\n{result.output}"
    )
