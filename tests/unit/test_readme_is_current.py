"""The README's checkable claims, checked.

A review in September 2026 found the README stale in five places at once: it
named MIT while LICENSE and pyproject.toml said Apache 2.0, listed five CLI
command groups out of sixteen, listed two installed packs out of five, and
described the corpus as empty when it held ninety thousand passages.

Every one of those was a hand-kept copy of something the code already
declares. Nothing could have caught the drift, because no test read the README.

These tests read it. They assert only the claims that have a machine-readable
source — the licence, the links, the commands — and deliberately say nothing
about the prose, which has no source to drift from.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from research_engine.cli.main import app

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
MAKEFILE = ROOT / "Makefile"
CORE_PYPROJECT = ROOT / "packages" / "core" / "pyproject.toml"

#: Fenced blocks are where the README makes executable claims. Prose that
#: happens to contain a backticked word is not a promise that it runs.
FENCED = re.compile(r"```[a-z]*\n(.*?)```", re.S)


def fenced_commands() -> str:
    return "\n".join(FENCED.findall(README.read_text()))


def cli_names() -> set[str]:
    """Every top-level name `research-engine` actually answers to."""
    names = {g.name for g in app.registered_groups if g.name}
    for command in app.registered_commands:
        # `@app.command()` with no argument takes the function's own name,
        # with underscores rendered as hyphens.
        names.add(command.name or command.callback.__name__.replace("_", "-"))
    return names


class TestTheReadmeMatchesWhatShips:
    def test_the_licence_is_the_one_the_project_declares(self) -> None:
        """The claim in the README with consequences outside the repo."""
        declared = tomllib.loads(CORE_PYPROJECT.read_text())["project"]["license"]
        assert declared == "Apache-2.0", "core's declared licence changed"

        body = README.read_text()
        section = body[body.index("## License") :]
        assert "Apache 2.0" in section, f"README licence section is stale: {section!r}"
        assert "MIT" not in section

    def test_every_relative_link_resolves(self) -> None:
        """A moved file should fail here, not in a reader's browser."""
        targets = re.findall(r"\]\(([^)]+)\)", README.read_text())
        missing = [
            t
            for t in targets
            if not t.startswith(("http://", "https://", "#"))
            and not (ROOT / t).exists()
        ]
        assert not missing, f"README links to paths that do not exist: {missing}"

    def test_every_research_engine_command_it_names_exists(self) -> None:
        """The list that went stalest: five groups named, sixteen registered."""
        named = set(re.findall(r"research-engine ([a-z][a-z-]*)", fenced_commands()))
        unknown = named - cli_names()
        assert not unknown, (
            f"README shows commands the CLI does not register: {sorted(unknown)}"
        )

    def test_every_make_target_it_names_exists(self) -> None:
        """The Quick Start delegates to the Makefile; the targets must be there."""
        targets = set(re.findall(r"^([a-z][a-z-]*):", MAKEFILE.read_text(), re.M))
        named = set(re.findall(r"\bmake ([a-z][a-z-]*)", fenced_commands()))
        unknown = named - targets
        assert not unknown, f"README shows make targets that do not exist: {sorted(unknown)}"
