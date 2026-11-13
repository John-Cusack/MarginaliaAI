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
