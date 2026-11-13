"""The readers must load before a job is read, or the job does not run.

The rule (design §7.2, revised): an empty per-region reading is legitimate only
when that reader is loaded and *ran*. A reader that cannot load contributes
silence to every page, and the run silently becomes an N-1 reader run whose gate
number was computed for three. So every configured reader is asked to read one
synthetic probe before the first page, and the job is blocked with the reader and
the error when one of them will not load.

No GPU and no images here: the readers are stubs, and the probe is generated.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from marginalia_ocr.server.app import PROTOCOL_HEADER, Settings, create_app
from marginalia_ocr.server.pipeline import (
    PROBE_SIZE,
    Readers,
    ReadersUnavailable,
    preflight,
    probe_image,
    require_readers,
)
from marginalia_ocr.server.store import Store
from marginalia_ocr.wire import PROTOCOL_VERSION

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from test_ocr_pipeline import Stub  # noqa: E402


def test_the_probe_is_a_synthetic_page_of_its_own():
    image = probe_image()
    assert image.size == PROBE_SIZE
    assert image.getpixel((PROBE_SIZE[0] // 2, PROBE_SIZE[1] // 2)) in {(0, 0, 0), (255, 255, 255)}


def test_a_reader_that_cannot_load_fails_its_preflight():
    verdicts = {v.reader: v for v in preflight(
        Readers(vlm=Stub(None, fails=True), tesseract=Stub("text"), third=None)
    )}
    assert verdicts["vlm"].ok is False
    assert "no model" in verdicts["vlm"].error
    assert verdicts["tesseract"].ok is True
    assert "third" not in verdicts, "a reader this host does not run is not asked"


def test_an_empty_answer_from_a_loaded_reader_still_passes_preflight():
    """The whole point of the distinction: silence is allowed, absence is not."""
    verdicts = preflight(Readers(vlm=Stub(None), tesseract=Stub(""), third=None))
    assert all(verdict.ok for verdict in verdicts)
    assert all(verdict.output_chars == 0 for verdict in verdicts)


def test_require_readers_refuses_and_names_what_is_missing():
    with pytest.raises(ReadersUnavailable) as raised:
        require_readers(Readers(vlm=Stub(None, fails=True), tesseract=Stub("text"), third=None))
    assert [v.reader for v in raised.value.unavailable] == ["vlm"]
    assert "vlm unavailable: RuntimeError: no model" in str(raised.value)


async def test_a_job_whose_reader_will_not_load_is_blocked_and_stores_nothing(tmp_path):
    """The failure the fix is for: pages would otherwise be stored, unmarked, with
    a reader missing, and nothing in the store would say so."""
    from ocr_synth.corpus import build_corpus

    settings = Settings(db_path=tmp_path / "jobs.sqlite3", page_store=tmp_path / "pages")
    store = Store(settings.db_path, settings.page_store)
    readers = Readers(
        vlm=Stub(None, fails=True),
        tesseract=Stub("the debt was 48.2% and the claim held"),
        third=Stub("the debt was 48.2% and the claim held"),
    )
    app = create_app(store=store, settings=settings, readers=readers)
    import httpx
    from marginalia_ocr.client import OcrClient, ScreenToUpload

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ocr")
    client = OcrClient("http://ocr", client=http)
    page = build_corpus(dprs=(1,), kinds=("two_column_prose",))[0]
    async with client:
        await client.create_job("SYNTHPF", dpr=1, pipeline_version="wp1")
        await client.upload_screen(
            ScreenToUpload(
                seq=0,
                capture_id="capture-0",
                scrubber=2614,
                layout_lines=44,
                dpr=3,
                png=page.png,
                scrubber_max=1933298,
                footer="Location 27 of 16166",
                page_data_start="27",
                page_data_end="28",
            )
        )
        response = await client._client.post(
            f"/v2/jobs/{client.job_id}/read", headers={PROTOCOL_HEADER: PROTOCOL_VERSION}
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "readers_unavailable"
    assert detail["readers"] == [{"reader": "vlm", "error": "RuntimeError: no model"}]
    row = store.job(client.job_id)
    assert row["state"] == "blocked"
    assert "reader vlm unavailable: RuntimeError: no model" in row["blocked_reason"]
    assert store.readings(client.job_id) == (), "a refused job stored readings"
    assert [c.read_at for c in store.captures(client.job_id)] == [None], "a page was marked read"
    assert readers.vlm.unloaded and readers.tesseract.unloaded, "the readers were not released"
