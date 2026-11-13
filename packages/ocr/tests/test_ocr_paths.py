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


SERVICE_UNIT = Path(__file__).resolve().parents[1] / "deploy" / "marginalia-ocr.service"


def service_environment(name: str) -> str:
    text = SERVICE_UNIT.read_text()
    match = re.search(rf"Environment={name}=(\S+)", text)
    assert match, f"{name} is not set in the unit"
    return match.group(1)


def test_the_service_unit_writes_the_same_root_as_the_sweeper():
    """The service writes pages; the timer deletes them. One root, or the timer
    sweeps an empty store while the images stay on disk."""
    root = service_environment("MARGINALIA_OCR_ROOT").replace("%h", str(Path.home()))
    assert Path(root) == Settings().page_store.parent
    unit_root = unit_argument("--page-store").parent
    assert Path(root) == unit_root


def test_the_service_unit_starts_on_demand_and_exits_when_idle():
    """Mirrors marginalia-embed.service: installed disabled, started by a run,
    and stopped by its own idle exit - so Restart must not become always."""
    text = SERVICE_UNIT.read_text()
    assert "Restart=on-failure" in text
    assert "Restart=always" not in text
    assert int(service_environment("MARGINALIA_OCR_IDLE_EXIT_AFTER")) > 0
    assert "--host 127.0.0.1" in text, "a page store does not belong on a public interface"


def test_the_service_unit_runs_the_app_factory_that_exists():
    """A typo in uvicorn's target is a unit that starts and immediately fails."""
    from marginalia_ocr.server import app as app_module

    command = re.search(r"ExecStart=(.*)", SERVICE_UNIT.read_text()).group(1).split()
    targets = [part for part in command[1:] if not part.startswith("-")]
    assert targets, "no uvicorn target in ExecStart"
    module_path, _, factory = targets[0].partition(":")
    assert module_path == "marginalia_ocr.server.app"
    assert hasattr(app_module, factory), factory


def test_the_units_run_the_ocr_checkout_and_never_the_embed_venv():
    """marginalia-embed runs from /home/john/repos/MarginaliaAI/.venv.

    Pointing the OCR units at that venv would change the environment of a service
    that is off-limits without John, so the paths are pinned here: a unit edited
    by hand or by a careless search-and-replace fails a test instead of quietly
    disturbing the embed server.
    """
    embed_venv = "/home/john/repos/MarginaliaAI/.venv"
    for unit in (SERVICE_UNIT, UNIT):
        text = unit.read_text()
        executables = re.findall(r"ExecStart=(\S+)", text)
        assert executables, unit.name
        for executable in executables:
            assert embed_venv not in executable, f"{unit.name} runs the embed venv"
            assert executable.startswith("/home/john/repos/marginalia-ocr-wp1/"), executable


def test_uvicorn_is_told_the_app_is_a_factory():
    """uvicorn guesses, noisily, and a guess is not a contract."""
    text = SERVICE_UNIT.read_text()
    assert "uvicorn --factory marginalia_ocr.server.app:app_from_env" in text


def test_the_service_does_not_take_a_port_another_service_is_using():
    """9882 is marginalia-embed, 9883 is the v1 Kindle OCR service.

    Both are other people's processes on the same host, and one of them is
    running. A careless edit here would collide at the first bind.
    """
    from marginalia_ocr.server.paths import DEFAULT_PORT

    assert DEFAULT_PORT not in {9882, 9883}
    text = SERVICE_UNIT.read_text()
    port = re.search(r"--port (\d+)", text)
    assert port, "no port in ExecStart"
    assert port.group(1) == str(DEFAULT_PORT)
    assert port.group(1) not in {"9882", "9883"}


def test_the_server_extra_declares_the_multipart_parser():
    """FastAPI's Form/File need python-multipart at *request* time, not import time.

    So a missing declaration survives every import test and every route-building
    test, and only fails when the first upload arrives. It did: the first deploy
    of this service under systemd died on startup with RuntimeError: Form data
    requires "python-multipart", in a venv built from exactly these extras.
    """
    import tomllib

    extra = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    requirements = extra["project"]["optional-dependencies"]["server"]
    assert any(requirement.startswith("python-multipart") for requirement in requirements), requirements
