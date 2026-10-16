"""A database backup must contain every schema needed by a fresh restore."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from research_engine.cli import backup

pytestmark = pytest.mark.unit


async def test_backup_does_not_filter_the_database_to_core(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import research_engine.config

    monkeypatch.setattr(
        research_engine.config,
        "load_settings",
        lambda: SimpleNamespace(
            db_url="postgresql+asyncpg://user:secret@localhost/research_engine"
        ),
    )
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backup.subprocess, "run", run)

    await backup._backup(tmp_path / "corpus.dump")  # noqa: SLF001

    assert calls == [[
        "pg_dump",
        "postgresql://user@localhost/research_engine",
        "-Fc",
        f"--file={tmp_path / 'corpus.dump'}",
    ]]
    assert not any(argument.startswith("--schema") for argument in calls[0])


def _settings_with(monkeypatch: pytest.MonkeyPatch, db_url: str) -> None:
    import research_engine.config

    monkeypatch.setattr(
        research_engine.config, "load_settings", lambda: SimpleNamespace(db_url=db_url)
    )


def _recording_run(
    monkeypatch: pytest.MonkeyPatch, returncode: int = 0
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append({"args": args, "env": kwargs.get("env")})
        return SimpleNamespace(returncode=returncode, stdout="", stderr="boom")

    monkeypatch.setattr(backup.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("command", ["backup", "restore"])
async def test_password_travels_in_the_environment_not_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path, command: str
) -> None:
    """argv is world-readable in the process table; PGPASSWORD is not."""
    _settings_with(monkeypatch, "postgresql+asyncpg://eng:s3cr%40t@127.0.0.1:55440/eng")
    calls = _recording_run(monkeypatch)

    if command == "backup":
        await backup._backup(tmp_path / "x.dump")  # noqa: SLF001
    else:
        await backup._restore(tmp_path / "x.dump")  # noqa: SLF001

    [call] = calls
    assert not any("s3cr" in argument for argument in call["args"])
    assert "postgresql://eng@127.0.0.1:55440/eng" in call["args"]
    assert call["env"]["PGPASSWORD"] == "s3cr@t"


async def test_no_password_leaves_pgpass_to_libpq(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("PGPASSWORD", raising=False)
    _settings_with(monkeypatch, "postgresql+asyncpg://eng@127.0.0.1:55440/eng")
    calls = _recording_run(monkeypatch)

    await backup._backup(tmp_path / "x.dump")  # noqa: SLF001

    assert "PGPASSWORD" not in calls[0]["env"]


async def test_a_failed_restore_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import typer

    _settings_with(monkeypatch, "postgresql+asyncpg://eng:pw@127.0.0.1:55440/eng")
    _recording_run(monkeypatch, returncode=1)

    with pytest.raises(typer.Exit) as excinfo:
        await backup._restore(tmp_path / "x.dump")  # noqa: SLF001
    assert excinfo.value.exit_code == 1
