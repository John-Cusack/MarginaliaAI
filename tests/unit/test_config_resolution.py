"""Config resolution: which file, and where each value came from.

The bug this guards: `env_file: ".env"` resolves against the process working
directory, so running the CLI from a different directory silently reads a
different file — or none — and a spend ceiling that fails to load looks exactly
like one that loaded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from research_engine.config.settings import (
    ENV_PREFIX,
    Settings,
    describe_settings,
    find_env_file,
    load_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real environment out of these tests."""
    for key in list(__import__("os").environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


def _project(tmp_path: Path, env_body: str | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    if env_body is not None:
        (tmp_path / ".env").write_text(env_body)
    return tmp_path


# --- Locating the file -----------------------------------------------------


def test_finds_env_file_in_the_starting_directory(tmp_path: Path) -> None:
    root = _project(tmp_path, "RE_DEFAULT_LANGUAGE=en\n")
    resolution = find_env_file(root)
    assert resolution.path == root / ".env"
    assert resolution.exists


def test_finds_env_file_from_a_subdirectory(tmp_path: Path) -> None:
    """The actual trap: running the CLI from anywhere below the project root."""
    root = _project(tmp_path, "RE_DEFAULT_LANGUAGE=en\n")
    deep = root / "packages" / "core" / "src"
    deep.mkdir(parents=True)

    resolution = find_env_file(deep)
    assert resolution.path == root / ".env"


def test_search_stops_at_the_project_root(tmp_path: Path) -> None:
    """It must not wander up into $HOME and read a stranger's .env."""
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".env").write_text("RE_DEFAULT_LANGUAGE=de\n")
    inner = _project(outer / "project")

    resolution = find_env_file(inner)
    assert resolution.path is None
    assert not resolution.exists


def test_missing_env_file_is_reported_not_guessed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    resolution = find_env_file(root)
    assert resolution.path is None
    assert "no .env found" in resolution.reason


def test_explicit_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path, "RE_DEFAULT_LANGUAGE=en\n")
    elsewhere = tmp_path / "other.env"
    elsewhere.write_text("RE_DEFAULT_LANGUAGE=fr\n")
    monkeypatch.setenv(f"{ENV_PREFIX}ENV_FILE", str(elsewhere))

    resolution = find_env_file(root)
    assert resolution.path == elsewhere.resolve()


def test_override_pointing_at_nothing_is_loud_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd RE_ENV_FILE must not silently fall through to a found .env."""
    root = _project(tmp_path, "RE_DEFAULT_LANGUAGE=en\n")
    monkeypatch.setenv(f"{ENV_PREFIX}ENV_FILE", str(tmp_path / "nope.env"))

    resolution = find_env_file(root)
    assert resolution.path is None
    assert "missing" in resolution.reason


# --- Loading ---------------------------------------------------------------


def test_values_load_from_the_resolved_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "custom.env"
    env.write_text("RE_DEFAULT_LANGUAGE=de\nRE_LLM_BUDGET_USD=12.5\n")
    monkeypatch.setenv(f"{ENV_PREFIX}ENV_FILE", str(env))

    settings = load_settings()
    assert settings.default_language == "de"
    assert settings.llm_budget_usd == 12.5


def test_environment_beats_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "custom.env"
    env.write_text("RE_DEFAULT_LANGUAGE=de\n")
    monkeypatch.setenv(f"{ENV_PREFIX}ENV_FILE", str(env))
    monkeypatch.setenv(f"{ENV_PREFIX}DEFAULT_LANGUAGE", "fr")

    assert load_settings().default_language == "fr"


# --- Reporting -------------------------------------------------------------


def test_report_attributes_each_value_to_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "custom.env"
    env.write_text("RE_DEFAULT_LANGUAGE=de\n")
    monkeypatch.setenv(f"{ENV_PREFIX}ENV_FILE", str(env))
    monkeypatch.setenv(f"{ENV_PREFIX}SEARCH_DEFAULT_K", "50")

    resolution = find_env_file(tmp_path)
    settings = Settings(_env_file=resolution.path)  # type: ignore[arg-type]
    by_name = {r.name: r for r in describe_settings(settings, resolution)}

    assert by_name["default_language"].source == "env file"
    assert by_name["search_default_k"].source == "environment"
    assert by_name["rrf_k"].source == "default"


def test_report_never_prints_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "custom.env"
    env.write_text("RE_ANTHROPIC_API_KEY=sk-ant-supersecret\n")
    monkeypatch.setenv(f"{ENV_PREFIX}ENV_FILE", str(env))

    resolution = find_env_file(tmp_path)
    settings = Settings(_env_file=resolution.path)  # type: ignore[arg-type]
    report = {r.name: r for r in describe_settings(settings, resolution)}["anthropic_api_key"]

    assert report.value == "SET"
    assert "supersecret" not in report.value
    assert report.source == "env file"


def test_a_value_equal_to_the_default_is_still_attributed_to_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact confusion this command exists to end.

    `.env` set db_url to the same string as the default, so comparing value to
    default said "default" and the file looked unread.
    """
    default_url = type(Settings()).model_fields["db_url"].default
    env = tmp_path / "custom.env"
    env.write_text(f"RE_DB_URL={default_url}\n")
    monkeypatch.setenv(f"{ENV_PREFIX}ENV_FILE", str(env))

    resolution = find_env_file(tmp_path)
    settings = Settings(_env_file=resolution.path)  # type: ignore[arg-type]
    report = {r.name: r for r in describe_settings(settings, resolution)}["db_url"]

    assert report.value == default_url
    assert report.source == "env file"


def test_env_file_keys_tolerate_comments_blanks_and_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "custom.env"
    env.write_text(
        "# a comment\n\n"
        "export RE_DEFAULT_LANGUAGE=en\n"
        "  RE_SEARCH_DEFAULT_K = 40 \n"
        "MALFORMED_LINE\n"
    )
    monkeypatch.setenv(f"{ENV_PREFIX}ENV_FILE", str(env))

    resolution = find_env_file(tmp_path)
    settings = Settings(_env_file=resolution.path)  # type: ignore[arg-type]
    by_name = {r.name: r for r in describe_settings(settings, resolution)}

    assert by_name["default_language"].source == "env file"
    assert by_name["search_default_k"].source == "env file"
