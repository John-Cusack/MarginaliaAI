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
        "postgresql://user:secret@localhost/research_engine",
        "-Fc",
        f"--file={tmp_path / 'corpus.dump'}",
    ]]
    assert not any(argument.startswith("--schema") for argument in calls[0])
