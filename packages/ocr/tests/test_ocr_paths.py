"""One root for the service and its sweeper.

The service defaults to a directory and the sweep unit names one in its
ExecStart. If they disagree, the timer sweeps an empty store while the images it
was written to delete live somewhere else - and the 14-day cap, which is a hard
cap and the only thing standing between a licensed page image and an indefinite
stay on disk, silently stops holding. The unit and the defaults are checked
against each other here, because nothing at runtime would notice.
"""

from __future__ import annotations

import re
from pathlib import Path

from marginalia_ocr.server.app import Settings

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "marginalia-ocr-sweep.service"


def unit_argument(name: str) -> Path:
    """The %h-expanded path the unit passes for one flag."""
    text = UNIT.read_text()
    match = re.search(rf"{name}\s+(\S+)", text)
    assert match, f"{name} is not in the unit"
    return Path(match.group(1).replace("%h", str(Path.home())))


def test_the_unit_sweeps_the_store_the_service_writes():
    settings = Settings.from_env({})
    assert unit_argument("--db") == settings.db_path
    assert unit_argument("--page-store") == settings.page_store


def test_the_default_root_is_one_a_user_unit_can_write():
    """A user unit cannot write /var/lib; a root there would never be used."""
    settings = Settings.from_env({})
    default = Settings().db_path
    assert settings.db_path == default
    assert str(default).startswith(str(Path.home())), default
    assert not str(default).startswith("/var/lib")
    assert settings.page_store == settings.db_path.parent / "pages"


def test_the_environment_can_still_move_the_store(tmp_path: Path):
    settings = Settings.from_env({"MARGINALIA_OCR_ROOT": str(tmp_path)})
    assert settings.db_path == tmp_path / "jobs.sqlite3"
    assert settings.page_store == tmp_path / "pages"
