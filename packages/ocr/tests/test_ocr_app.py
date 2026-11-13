"""The service's HTTP surface, and the promise behind a CommitAck.

The upload path is where a walk's durable progress is decided: the pack treats
the ack as "this screen survives a crash", so the ack must not exist until the
bytes do. The last test here kills the process immediately after the ack and
re-opens the store, which is the only way to test that claim honestly.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from typing import TYPE_CHECKING

import httpx
import pytest
from marginalia_ocr.client import OcrClient, ScreenToUpload
from marginalia_ocr.server.app import PROTOCOL_HEADER, Settings, create_app
from marginalia_ocr.server.store import Store
from marginalia_ocr.wire import PROTOCOL_VERSION, ProtocolMismatch
from pngs import chunk, png_bytes

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "jobs.sqlite3", page_store=tmp_path / "pages")


@pytest.fixture()
def app(settings: Settings):
    return create_app(store=Store(settings.db_path, settings.page_store), settings=settings)


def client_for(app) -> OcrClient:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ocr")
    return OcrClient("http://ocr", client=http)


def screen(png: bytes, seq: int = 0) -> ScreenToUpload:
    return ScreenToUpload(
        seq=seq,
        capture_id=f"capture-{seq}",
        scrubber=2614 + seq,
        layout_lines=44,
        dpr=3,
        png=png,
        scrubber_max=1933298,
        footer="Location 27 of 16166",
        page_data_start="27",
        page_data_end="28",
    )


async def test_a_job_is_created_and_reported(settings: Settings):
    async with client_for(create_app(store=Store(settings.db_path, settings.page_store), settings=settings)) as client:
        status = await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        assert status.state == "captured"
        assert status.captures_committed == 0
        assert (await client.status()).job_id == status.job_id


async def test_a_screen_upload_acks_only_after_the_bytes_are_stored(settings: Settings):
    store = Store(settings.db_path, settings.page_store)
    async with client_for(create_app(store=store, settings=settings)) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        png = png_bytes(1280, 780)
        ack = await client.upload_screen(screen(png))
        assert ack.committed is True
        assert ack.capture_id == "capture-0"

        captures = store.captures(ack.job_id)
        assert len(captures) == 1
        assert captures[0].path.read_bytes() == png
        assert captures[0].image_sha256 == hashlib.sha256(png).hexdigest()
        status = await client.status()
        assert status.captures_committed == 1
        assert status.last_committed_position == 0


async def test_a_protocol_mismatch_writes_nothing(settings: Settings):
    store = Store(settings.db_path, settings.page_store)
    app = create_app(store=store, settings=settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ocr") as http:
        response = await http.post(
            "/v2/jobs",
            json={"asin": "B0", "dpr": 3, "pipeline_version": "wp1"},
            headers={PROTOCOL_HEADER: "1"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "protocol_mismatch"
    assert store.job_ids() == (), "a refused version must not create a job"


async def test_the_client_turns_a_version_mismatch_into_an_exception(settings: Settings):
    app = create_app(store=Store(settings.db_path, settings.page_store), settings=settings)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ocr")
    client = OcrClient("http://ocr", client=http, job_id="x")
    from marginalia_ocr.wire import JobCreate

    # A wrong-version server response is a ProtocolMismatch, never a retry and
    # never an empty read: the pack stops instead of storing a blank page.
    response = httpx.Response(
        409,
        json={"error": "protocol_mismatch", "got": "1", "want": "2"},
        request=httpx.Request("POST", "http://ocr/v2/jobs"),
    )
    with pytest.raises(ProtocolMismatch):
        client._raise_for_status(response)
    assert JobCreate(asin="B0", dpr=3, pipeline_version="v").protocol == PROTOCOL_VERSION


async def test_a_bad_image_is_refused_with_a_reason(settings: Settings):
    async with client_for(create_app(store=Store(settings.db_path, settings.page_store), settings=settings)) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        bad = screen(png_bytes())
        bad.png = b"not a png at all"
        with pytest.raises(Exception) as caught:
            await client.upload_screen(bad)
        assert "image_rejected" in str(caught.value)

        animated = screen(png_bytes())
        raw = animated.png
        animated.png = raw[:33] + chunk(b"acTL", b"\x00" * 8) + raw[33:]
        with pytest.raises(Exception) as caught:
            await client.upload_screen(animated)
        assert "animated png" in str(caught.value)


async def test_a_bundle_is_not_available_before_it_is_written(settings: Settings):
    async with client_for(create_app(store=Store(settings.db_path, settings.page_store), settings=settings)) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        with pytest.raises(Exception) as caught:
            await client.bundle()
        assert "no_bundle" in str(caught.value)


async def test_a_job_walks_to_final_and_its_images_are_gone(settings: Settings):
    store = Store(settings.db_path, settings.page_store)
    async with client_for(create_app(store=store, settings=settings)) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        ack = await client.upload_screen(screen(png_bytes()))
        for state in ("read", "checked", "assembled", "audited", "bundle_written"):
            await client.advance(state)
        store.set_bundle_hash(ack.job_id, "c" * 64)

        bundle = await client.bundle()
        assert bundle.state == "bundle_written"
        assert bundle.captures and bundle.bundle_sha256 == "c" * 64

        final = await client.finalize()
        assert final.state == "final"
        assert not (settings.page_store / final.job_id).exists()
        assert store.captures(final.job_id) == ()


async def test_finalize_is_refused_from_any_other_state(settings: Settings):
    store = Store(settings.db_path, settings.page_store)
    app = create_app(store=store, settings=settings)
    async with client_for(app) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        await client.upload_screen(screen(png_bytes()))
        response = await client._client.post(
            f"/v2/jobs/{client.job_id}/finalize", headers={PROTOCOL_HEADER: PROTOCOL_VERSION}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "not_ready_to_finalize"


def test_a_crash_right_after_the_ack_leaves_the_screen_committed(tmp_path: Path):
    """The ack is a durability claim, so the test has to kill something.

    A child process uploads one screen over the real HTTP surface and calls
    os._exit(0) the moment the ack arrives - no cleanup, no flush, no atexit. If
    anything about the write were buffered, the row or the file would be missing
    here, and the pack's resume would start from the wrong position.
    """
    script = """
import asyncio, hashlib, json, os, sys
import httpx
from marginalia_ocr.client import OcrClient, ScreenToUpload
from marginalia_ocr.server.app import Settings, create_app
from marginalia_ocr.server.store import Store
from PIL import Image
import io

root = sys.argv[1]
buffer = io.BytesIO()
Image.new("RGB", (1280, 780), "white").save(buffer, format="PNG")
png = buffer.getvalue()

async def main():
    settings = Settings(db_path=__import__("pathlib").Path(root) / "jobs.sqlite3",
                        page_store=__import__("pathlib").Path(root) / "pages")
    app = create_app(store=Store(settings.db_path, settings.page_store), settings=settings)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ocr")
    client = OcrClient("http://ocr", client=http)
    await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
    ack = await client.upload_screen(ScreenToUpload(seq=0, capture_id="c0", scrubber=2614,
                                                    layout_lines=44, dpr=3, png=png))
    assert ack.committed
    sys.stdout.write(json.dumps({"job_id": ack.job_id, "sha": hashlib.sha256(png).hexdigest()}))
    sys.stdout.flush()
    os._exit(0)   # the crash: nothing after this line runs, ever

asyncio.run(main())
"""
    root = tmp_path
    finished = subprocess.run(
        [sys.executable, "-c", script, str(root)], capture_output=True, text=True, check=True
    )
    payload = json.loads(finished.stdout)
    assert finished.returncode == 0

    # A fresh store, opened after the crash: the screen must be here.
    store = Store(root / "jobs.sqlite3", root / "pages")
    captures = store.captures(payload["job_id"])
    assert len(captures) == 1, "the ack was sent for a screen that did not survive"
    assert captures[0].image_sha256 == payload["sha"]
    assert captures[0].path.exists()
    assert hashlib.sha256(captures[0].path.read_bytes()).hexdigest() == payload["sha"]
    assert store.transitions(payload["job_id"]) == ("captured",)


async def test_an_unknown_job_is_a_404(settings: Settings):
    app = create_app(store=Store(settings.db_path, settings.page_store), settings=settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ocr") as http:
        response = await http.get("/v2/jobs/nope", headers={PROTOCOL_HEADER: PROTOCOL_VERSION})
    assert response.status_code == 404


async def test_healthz_reports_the_bounds_it_will_enforce(settings: Settings):
    app = create_app(store=Store(settings.db_path, settings.page_store), settings=settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ocr") as http:
        body = (await http.get("/healthz")).json()
    assert body["ok"] is True
    assert body["pixel_cap"] >= 9_500_000
    assert body["ttl_days"] == 14


async def test_the_earliest_live_image_is_the_expiry_that_is_reported(settings: Settings):
    """A resume decision reads this: the first image about to go, not the clock."""
    store = Store(settings.db_path, settings.page_store)
    async with client_for(create_app(store=store, settings=settings)) as client:
        fresh = await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        assert fresh.images_expire_at is None or fresh.captures_committed == 0
        first = await client.upload_screen(screen(png_bytes(), seq=0))
        second = await client.upload_screen(screen(png_bytes(colour="grey"), seq=1))

        status = await client.status()
        expiries = [capture.expire_at for capture in store.captures(status.job_id)]
        assert status.images_expire_at == min(expiries)
        assert status.images_expire_at == first.images_expire_at
        assert second.images_expire_at >= first.images_expire_at


async def test_creating_the_same_job_twice_returns_the_same_job(settings: Settings):
    """The pack re-creates its job on every resume; that is not an error."""
    async with client_for(create_app(store=Store(settings.db_path, settings.page_store), settings=settings)) as client:
        first = await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        await client.upload_screen(screen(png_bytes()))
        again = await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        assert again.job_id == first.job_id
        assert again.state == "captured"
        assert again.captures_committed == 1, "the resumed job knows what it already has"


async def test_creating_a_job_with_a_different_shape_is_a_conflict(settings: Settings):
    async with client_for(create_app(store=Store(settings.db_path, settings.page_store), settings=settings)) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        with pytest.raises(Exception) as caught:
            await client.create_job("B004774792", dpr=2, pipeline_version="wp1")
        assert "job_conflict" in str(caught.value)


async def test_uploads_are_refused_once_the_book_is_past_capture(settings: Settings):
    """A late image would sit on disk for 14 days behind a finished book."""
    store = Store(settings.db_path, settings.page_store)
    async with client_for(create_app(store=store, settings=settings)) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        await client.upload_screen(screen(png_bytes(), seq=0))
        await client.advance("read")

        with pytest.raises(Exception) as caught:
            await client.upload_screen(screen(png_bytes(), seq=1))
        assert "not_accepting_uploads" in str(caught.value)


async def test_an_upload_is_allowed_for_a_position_that_owes_a_recapture(settings: Settings):
    store = Store(settings.db_path, settings.page_store)
    async with client_for(create_app(store=store, settings=settings)) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        await client.upload_screen(screen(png_bytes(), seq=0))
        await client.advance("read")
        store.request_recapture(client.job_id, 1, "image expired before the page was finalized", 3)

        ack = await client.upload_screen(screen(png_bytes(colour="grey"), seq=1))
        assert ack.committed is True


async def test_an_oversized_body_is_refused_without_being_buffered(settings: Settings):
    """The bound has to be on the read, not on the parse: the point is the memory.

    A client that streams 4 GB at an endpoint that buffers first has already won
    by the time validation runs, so the endpoint reads max_bytes + 1 and refuses
    on that.
    """
    settings = Settings(
        db_path=settings.db_path, page_store=settings.page_store, max_bytes=1024
    )
    app = create_app(store=Store(settings.db_path, settings.page_store), settings=settings)
    async with client_for(app) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        huge = screen(png_bytes(2000, 2000))
        with pytest.raises(Exception) as caught:
            await client.upload_screen(huge)
        assert "too large" in str(caught.value)


async def test_the_status_of_a_job_with_no_images_has_no_expiry(settings: Settings):
    async with client_for(create_app(store=Store(settings.db_path, settings.page_store), settings=settings)) as client:
        status = await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
        assert status.images_expire_at is None


async def test_the_idle_watchdog_stops_the_service_after_quiet(settings: Settings):
    """Started on demand, stopped when nobody is asking: a GPU host is not a
    place to leave a process waiting."""
    import asyncio
    import time

    from marginalia_ocr.server.app import create_app, idle_watchdog

    fired: list[float] = []
    app = create_app(
        store=Store(settings.db_path, settings.page_store),
        settings=Settings(
            db_path=settings.db_path, page_store=settings.page_store, idle_exit_after=0.05
        ),
        on_idle=lambda: fired.append(time.monotonic()),
    )
    task = asyncio.create_task(idle_watchdog(app, interval=0.01, on_idle=lambda: fired.append(1.0)))
    await asyncio.sleep(0.2)
    assert fired, "an idle service never stopped itself"
    task.cancel()


async def test_the_idle_watchdog_leaves_a_busy_service_alone(settings: Settings):
    import asyncio
    import time

    from marginalia_ocr.server.app import create_app, idle_watchdog

    fired: list[int] = []
    app = create_app(
        store=Store(settings.db_path, settings.page_store),
        settings=Settings(
            db_path=settings.db_path, page_store=settings.page_store, idle_exit_after=0.3
        ),
        on_idle=lambda: fired.append(1),
    )
    task = asyncio.create_task(idle_watchdog(app, interval=0.01, on_idle=lambda: fired.append(1)))
    try:
        for _ in range(5):
            app.state.last_request = time.monotonic()
            await asyncio.sleep(0.05)
        assert fired == [], "a service with work in flight stopped itself"
    finally:
        task.cancel()


async def test_a_request_marks_the_service_busy(settings: Settings):
    """The watchdog reads this clock, so a served request has to move it."""
    app = create_app(store=Store(settings.db_path, settings.page_store), settings=settings)
    before = app.state.last_request
    async with client_for(app) as client:
        await client.create_job("B004774792", dpr=3, pipeline_version="wp1")
    assert app.state.last_request > before


def stub_readers(text: str = "the debt was 48.2% and the claim held"):
    """Three readers that agree, so an app test is about the store, not a model."""
    from marginalia_ocr.server.pipeline import Readers
    from test_ocr_pipeline import Stub

    return Readers(vlm=Stub(text), tesseract=Stub(text), third=Stub(text))


async def test_the_read_trigger_reads_every_screen_and_keeps_its_gaps(settings: Settings):
    """The minimal read path: inventory the page, read its regions, store both.

    Synthetic screens, not Kindle ones, and no images or text in the log.
    """
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "tools"))
    from ocr_synth.corpus import build_corpus

    screens = build_corpus(dprs=(1,), kinds=("two_column_prose", "notes_page"))
    store = Store(settings.db_path, settings.page_store)
    async with client_for(
        create_app(store=store, settings=settings, readers=stub_readers())
    ) as client:
        await client.create_job("SYNTH1", dpr=1, pipeline_version="wp1")
        for seq, page in enumerate(screens):
            await client.upload_screen(screen(page.png, seq=seq))

        response = await client._client.post(
            f"/v2/jobs/{client.job_id}/read", headers={PROTOCOL_HEADER: PROTOCOL_VERSION}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["read"] == 2
        assert body["regions"] > 0

        readings = store.readings(client.job_id)
        assert readings, "the trigger read nothing"
        assert all(row["kind"] != "chrome" for row in readings), "chrome was read as text"
        assert any(row["text"].strip() for row in readings), "no text came out of a page of prose"
        assert all(store.captures(client.job_id)[i].read_at is not None for i in range(2))

        again = (
            await client._client.post(
                f"/v2/jobs/{client.job_id}/read", headers={PROTOCOL_HEADER: PROTOCOL_VERSION}
            )
        ).json()
        assert again["read"] == 0, "a second read of the same screens is not a second read"


async def test_the_watchdog_leaves_a_service_with_work_in_flight_alone(settings: Settings):
    """A background read sends no requests, and must not be mistaken for idle.

    Without this, the service stops itself mid-book: the read is running, the pack
    is waiting, and the idle clock says nothing has happened for fifteen minutes.
    """
    import asyncio
    import time

    from marginalia_ocr.server.app import create_app, idle_watchdog

    fired: list[int] = []
    app = create_app(
        store=Store(settings.db_path, settings.page_store),
        settings=Settings(
            db_path=settings.db_path, page_store=settings.page_store, idle_exit_after=0.05
        ),
        on_idle=lambda: fired.append(1),
    )
    app.state.work.begin()          # a read is running
    app.state.last_request = time.monotonic() - 10  # and no requests are arriving
    task = asyncio.create_task(idle_watchdog(app, interval=0.01, on_idle=lambda: fired.append(1)))
    try:
        await asyncio.sleep(0.2)
        assert fired == [], "the service stopped itself with work in flight"

        app.state.work.end()
        await asyncio.sleep(0.2)
        assert fired, "and then it never stopped, with the work done"
    finally:
        task.cancel()


async def test_the_status_counts_what_was_read_and_what_could_not_be(settings: Settings):
    """Counts, never text: the pack needs to know a read happened, not what it said."""
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "tools"))
    from ocr_synth.corpus import build_corpus

    page = build_corpus(dprs=(1,), kinds=("two_column_prose",))[0]
    store = Store(settings.db_path, settings.page_store)
    async with client_for(
        create_app(store=store, settings=settings, readers=stub_readers())
    ) as client:
        await client.create_job("SYNTH2", dpr=1, pipeline_version="wp1")
        await client.upload_screen(screen(page.png, seq=0))
        before = await client.status()
        assert (before.readings, before.gaps) == (0, 0)

        await client._client.post(
            f"/v2/jobs/{client.job_id}/read", headers={PROTOCOL_HEADER: PROTOCOL_VERSION}
        )
        after = await client.status()
        assert after.readings > 0
        assert after.gaps == 0, "a clean synthetic page has no unplaceable ink"
