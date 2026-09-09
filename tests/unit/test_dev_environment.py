"""The dev database's port is written in four files; they must agree.

`make db-status` used to run `pg_isready -h localhost` with libpq's default
port, 5432, while the container publishes 5435. It therefore reported "DB is
down" against a database that was up and healthy — and, worse, said nothing
about which port it had checked, so the message was indistinguishable from a
real outage. Two sessions lost time to it before it was traced.

Nothing could have caught the drift, because the port is a literal in a
Makefile, a compose file, a Python fallback and a README. These read all four
and compare them, so the next edit to any one of them fails here rather than in
someone's terminal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "tools" / "dev-postgres" / "docker-compose.yml"
MAKEFILE = ROOT / "Makefile"
README = ROOT / "README.md"
TESTING_DB = (
    ROOT / "packages" / "core" / "src" / "research_engine" / "testing" / "database.py"
)


def published_port() -> str:
    """The host port `docker compose up` binds, from the compose file itself."""
    match = re.search(r'"(\d+):5432"', COMPOSE.read_text())
    assert match, f"no host:5432 port mapping in {COMPOSE}"
    return match.group(1)


class TestThePortIsWrittenOnceAndCopiedFaithfully:
    def test_the_makefile_checks_the_port_the_container_publishes(self) -> None:
        match = re.search(r"^DB_PORT := (\d+)$", MAKEFILE.read_text(), re.M)
        assert match, "Makefile no longer defines DB_PORT"
        assert match.group(1) == published_port()

    def test_db_status_names_the_port_it_checked(self) -> None:
        """A bare "DB is down" cannot be told from "I looked in the wrong place"."""
        body = MAKEFILE.read_text()
        target = body[body.index("db-status:") : body.index("ALEMBIC_INI")]
        assert "-p $(DB_PORT)" in target, "db-status must pass an explicit port"
        assert target.count("$(DB_PORT)") >= 3, "both outcomes must name the port"

    def test_the_test_database_fallback_uses_the_same_port(self) -> None:
        """Packs inherit this URL, so a wrong port here skips their whole suite."""
        urls = re.findall(r"localhost:(\d+)/research_engine", TESTING_DB.read_text())
        assert urls, "no fallback dev URL in testing/database.py"
        assert set(urls) == {published_port()}

    def test_the_readme_tells_a_newcomer_the_right_port(self) -> None:
        urls = re.findall(r"localhost:(\d+)/research_engine", README.read_text())
        assert urls, "the README no longer shows an RE_DB_URL"
        assert set(urls) == {published_port()}
