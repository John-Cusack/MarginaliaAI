"""A deployed MCP profile refuses configuration it did not choose.

The bug this guards: a reference server whose env file failed to load would
attach to the built-in development database, and one started from inside a
checkout would read that checkout's `.env` — both silently.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from research_engine.config.settings import ENV_PREFIX, load_settings
from research_engine.domain.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

ENG_URL = "postgresql+asyncpg://eng:pw@127.0.0.1:55440/marginalia_eng"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


def _project(tmp_path: Path, env_body: str) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / ".env").write_text(env_body)
    return tmp_path


def test_explicit_env_file_with_a_real_database_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "eng.env"
    env.write_text(f"RE_DB_URL={ENG_URL}\nRE_MCP_PROFILE=reference\n")
    monkeypatch.setenv("RE_ENV_FILE", str(env))
    settings = load_settings()
    assert settings.mcp_profile == "reference"
    assert settings.db_url == ENG_URL


def test_profile_from_a_walked_up_env_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, f"RE_DB_URL={ENG_URL}\nRE_MCP_PROFILE=reference\n")
    monkeypatch.chdir(root)
    with pytest.raises(ConfigurationError, match="RE_ENV_FILE"):
        load_settings()


def test_profile_on_the_development_database_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "eng.env"
    env.write_text("RE_MCP_PROFILE=reference\n")
    monkeypatch.setenv("RE_ENV_FILE", str(env))
    with pytest.raises(ConfigurationError, match="development database"):
        load_settings()


def test_profile_with_a_missing_env_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RE_ENV_FILE", str(tmp_path / "nope.env"))
    monkeypatch.setenv("RE_MCP_PROFILE", "reference")
    monkeypatch.setenv("RE_DB_URL", ENG_URL)
    with pytest.raises(ConfigurationError, match="existing file"):
        load_settings()


def test_serve_with_a_launch_profile_and_missing_env_file_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented wrapper: profile in the process env, env file moved away."""
    import research_engine.runtime as runtime
    from research_engine.cli.main import app

    touched: list[str] = []

    async def _build_container(*_args: object, **_kwargs: object) -> None:
        touched.append("build_container")

    async def _run_mcp_server(*_args: object, **_kwargs: object) -> None:
        touched.append("run_mcp_server")

    monkeypatch.setattr(runtime, "build_container", _build_container)
    monkeypatch.setattr(runtime, "run_mcp_server", _run_mcp_server)
    monkeypatch.setenv("RE_ENV_FILE", str(tmp_path / "moved.env"))
    monkeypatch.setenv("RE_MCP_PROFILE", "reference")

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigurationError)
    assert "RE_ENV_FILE" in str(result.exception)
    assert touched == []


def test_tool_list_alone_also_turns_the_guard_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, f"RE_DB_URL={ENG_URL}\nRE_MCP_TOOLS=find_passages\n")
    monkeypatch.chdir(root)
    with pytest.raises(ConfigurationError):
        load_settings()


def test_profile_from_the_environment_alone_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env file at all is fine: a unit's Environment= is a choice too."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RE_MCP_PROFILE", "reference")
    monkeypatch.setenv("RE_DB_URL", ENG_URL)
    assert load_settings().db_url == ENG_URL


def test_blank_tool_list_in_a_development_env_keeps_walk_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "RE_MCP_TOOLS=\nRE_LOG_LEVEL=DEBUG\n")
    monkeypatch.chdir(root)
    assert load_settings().log_level == "DEBUG"


def test_development_shell_keeps_walk_up_and_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "RE_LOG_LEVEL=DEBUG\n")
    monkeypatch.chdir(root)
    assert load_settings().log_level == "DEBUG"
