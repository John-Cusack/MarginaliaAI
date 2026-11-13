"""The HTTP service: jobs in, screens in, durable commits out.

Three rules shape this file.

* **Nothing is acked before it is durable.** The page store fsyncs the image and
  its directory before the store records the capture, and only then does the
  client get its `CommitAck`. The pack's resume logic treats that ack as "this
  screen survives a crash", so it has to mean exactly that.
* **The protocol is rejected, not negotiated.** A client speaking another version
  gets 409 and nothing is written: a page stored under rules the client did not
  expect is worse than a page not stored.
* **`final` waits for deletion.** Finalization deletes the images, verifies they
  are gone, and only then advances - and the store refuses `final` while any
  image remains, whatever the caller asks.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ..wire import (
    PROTOCOL_VERSION,
    STATES,
    Bundle,
    CommitAck,
    JobCreate,
    JobStatus,
    ProtocolMismatch,
    RecaptureRequest,
    ScreenUpload,
    State,
    check_protocol,
)
from .confirm import Vocabulary
from .paths import DEFAULT_ROOT
from .pipeline import ReaderFailed, preflight, read_page
from .pngcheck import (
    DEFAULT_MAX_BYTES,
    DEFAULT_PIXEL_CAP,
    MIN_PIXEL_CAP,
    PngRejected,
    validate_image,
)
from .store import IMAGE_TTL_DAYS, CaptureConflict, Store, StoreError, UnknownJob

PROTOCOL_HEADER = "X-Ocr-Protocol"


@dataclass
class Settings:
    """What the service is allowed to accept and how long it keeps it."""

    db_path: Path = DEFAULT_ROOT / "jobs.sqlite3"
    page_store: Path = DEFAULT_ROOT / "pages"
    pixel_cap: int = DEFAULT_PIXEL_CAP
    max_bytes: int = DEFAULT_MAX_BYTES
    ttl_days: int = IMAGE_TTL_DAYS
    #: Stop after this many seconds with no request; 0 disables it. The service is
    #: started on demand by the pack and must not hold a GPU host awake.
    idle_exit_after: float = 900.0
    #: Which readers to run. The VLM needs the GPU extra and the lease; disabling
    #: it here is how a host without a card still serves the other two.
    readers: str = "vlm,tesseract,third"
    #: Which reading is stored when the readers disagree and no tie-break applies.
    #: Chosen by *measured prose word error rate*, not by which model was chosen in
    #: Phase 0 for a different job: on the 3x synthetic set Tesseract's is 0.070 and
    #: the VLM's was 4.349 before it was bounded, so the spine is Tesseract and the
    #: mark names it either way.
    primary: str = "tesseract"
    read_note_links: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        root = Path(env.get("MARGINALIA_OCR_ROOT", str(DEFAULT_ROOT)))
        return cls(
            db_path=Path(env.get("MARGINALIA_OCR_DB", root / "jobs.sqlite3")),
            page_store=Path(env.get("MARGINALIA_OCR_PAGES", root / "pages")),
            pixel_cap=int(env.get("MARGINALIA_OCR_PIXEL_CAP", DEFAULT_PIXEL_CAP)),
            max_bytes=int(env.get("MARGINALIA_OCR_MAX_BYTES", DEFAULT_MAX_BYTES)),
            ttl_days=int(env.get("MARGINALIA_OCR_TTL_DAYS", IMAGE_TTL_DAYS)),
            idle_exit_after=float(env.get("MARGINALIA_OCR_IDLE_EXIT_AFTER", 900.0)),
            readers=env.get("MARGINALIA_OCR_READERS", "vlm,tesseract,third"),
            primary=env.get("MARGINALIA_OCR_PRIMARY", "tesseract"),
            read_note_links=env.get("MARGINALIA_OCR_NOTE_LINKS", "1") not in {"0", "false", ""},
        )

    def validate(self) -> Settings:
        if self.pixel_cap < MIN_PIXEL_CAP:
            raise ValueError(
                f"pixel cap {self.pixel_cap} is under {MIN_PIXEL_CAP}: 3x screens would be refused"
            )
        if self.ttl_days > IMAGE_TTL_DAYS:
            raise ValueError(f"the {IMAGE_TTL_DAYS}-day image cap is a cap, not a default")
        return self


def _status(store: Store, job_id: str) -> JobStatus:
    row = store.job(job_id)
    captures = store.captures(job_id, include_discarded=True)
    return JobStatus(
        job_id=job_id,
        asin=row["asin"],
        state=row["state"],
        captures_committed=sum(1 for capture in captures if not capture.discarded),
        readings=len(store.readings(job_id)),
        gaps=len(store.gaps(job_id)),
        last_committed_position=store.last_committed_seq(job_id),
        # The first image that will go, not the time of asking: a resume
        # decision reads this.
        images_expire_at=store.earliest_expiry(job_id),
        recapture=tuple(
            RecaptureRequest(position=position, reason=reason, dpr=dpr)
            for position, reason, dpr in store.recapture_requests(job_id)
        ),
        error=row["error"],
    )


class InFlight:
    """Work the service is doing, whether or not a request is arriving.

    The idle exit used to look only at HTTP traffic, which is wrong the moment a
    read runs in the background: a book mid-read sends no requests, so the service
    would decide it was idle and stop itself with the page half done. WP2's
    background stages register here; the watchdog will not leave while anything is
    running.

    Single-threaded by construction: everything that touches this runs on the
    event loop.
    """

    def __init__(self) -> None:
        self.count = 0
        self.last_change = time.monotonic()

    def begin(self) -> InFlight:
        self.count += 1
        self.last_change = time.monotonic()
        return self

    def end(self) -> None:
        self.count = max(0, self.count - 1)
        self.last_change = time.monotonic()

    def __enter__(self) -> InFlight:
        return self.begin()

    def __exit__(self, *exc_info: object) -> None:
        self.end()


async def idle_watchdog(app: FastAPI, *, interval: float = 5.0, on_idle=None):
    """Stop the service once nothing has asked it for work.

    Separate from the app so it can be tested without killing the test runner: the
    default action is to ask systemd to stop us cleanly, which is what keeps the
    GPU host free between books.
    """
    import asyncio
    import signal

    action = on_idle or (lambda: os.kill(os.getpid(), signal.SIGTERM))
    while True:
        await asyncio.sleep(interval)
        if app.state.work.count:
            continue  # a read is running: not idle, whatever the requests say
        busy_since = max(app.state.last_request, app.state.work.last_change)
        idle_for = time.monotonic() - busy_since
        if idle_for >= app.state.settings.idle_exit_after:
            app.state.idle_at = time.monotonic()
            action()
            return


def lifespan_for(settings: Settings, on_idle=None):
    """Start the idle watchdog with the service, and stop it with the service."""
    import asyncio
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = (
            asyncio.create_task(idle_watchdog(app, on_idle=on_idle))
            if settings.idle_exit_after
            else None
        )
        app.state.watchdog = task
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    return lifespan


def build_readers(settings: Settings):
    """The three readers this host is configured to run, built lazily.

    Imported here rather than at module scope: the VLM pulls in torch, and a host
    serving only Tesseract must not need it.
    """
    from .pipeline import Readers
    from .readers.tesseract import TesseractReader

    wanted = {name.strip() for name in settings.readers.split(",") if name.strip()}
    vlm = None
    if "vlm" in wanted:
        from .readers.vlm import VlmReader

        vlm = VlmReader()
    third = None
    if "third" in wanted:
        from .readers.third import EasyOcrReader

        third = EasyOcrReader()
    return Readers(vlm=vlm, tesseract=TesseractReader(), third=third)


def create_app(
    store: Store | None = None, settings: Settings | None = None, on_idle=None, readers=None
) -> FastAPI:
    settings = (settings or Settings.from_env()).validate()
    store = store or Store(settings.db_path, settings.page_store)
    app = FastAPI(
        title="marginalia-ai-ocr", version=PROTOCOL_VERSION, lifespan=lifespan_for(settings, on_idle)
    )
    app.state.store = store
    app.state.settings = settings
    app.state.last_request = time.monotonic()
    app.state.idle_at = None
    app.state.work = InFlight()
    app.state.readers = readers

    @app.middleware("http")
    async def _touch(request, call_next):
        app.state.last_request = time.monotonic()
        app.state.work.begin()
        try:
            return await call_next(request)
        finally:
            app.state.work.end()


    def require_protocol(header: str | None) -> None:
        try:
            check_protocol(header)
        except ProtocolMismatch as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "protocol_mismatch", "got": exc.got, "want": exc.want},
            ) from exc

    @app.exception_handler(PngRejected)
    async def _png_rejected(_request, exc: PngRejected) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"error": "image_rejected", "reason": exc.reason, "detail": exc.detail}
        )

    @app.exception_handler(UnknownJob)
    async def _unknown_job(_request, exc: UnknownJob) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "unknown_job", "detail": str(exc)})

    @app.exception_handler(CaptureConflict)
    async def _conflict(_request, exc: CaptureConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "capture_conflict", "detail": str(exc)})

    @app.exception_handler(StoreError)
    async def _store_error(_request, exc: StoreError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "state_error", "detail": str(exc)})

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "pixel_cap": settings.pixel_cap,
            "ttl_days": settings.ttl_days,
        }

    @app.post("/v2/jobs", response_model=JobStatus)
    async def create_job(
        job: JobCreate, x_ocr_protocol: str | None = Header(default=None)
    ) -> JobStatus:
        require_protocol(x_ocr_protocol)
        job_id = f"{job.asin}-{job.pipeline_version}"
        # The pack re-creates its job on every resume, so this is the ordinary
        # path, not an error: an existing job answers with its status - including
        # how many screens it already holds - unless the book's shape changed,
        # which would make "the job" mean two different walks.
        existing = store.find_job(job_id)
        if existing is not None:
            if existing["asin"] != job.asin or existing["dpr"] != job.dpr:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "job_conflict",
                        "detail": f"{job_id} exists with asin={existing['asin']} dpr={existing['dpr']}",
                    },
                )
            return _status(store, job_id)
        store.create_job(
            job_id=job_id,
            asin=job.asin,
            dpr=job.dpr,
            pipeline_version=job.pipeline_version,
            title=job.title,
        )
        return _status(store, job_id)

    @app.post("/v2/jobs/{job_id}/screens", response_model=CommitAck)
    async def upload_screen(
        job_id: str,
        metadata: str = Form(...),
        image: UploadFile = File(...),
        x_ocr_protocol: str | None = Header(default=None),
    ) -> CommitAck:
        require_protocol(x_ocr_protocol)
        try:
            parsed = ScreenUpload.model_validate(json.loads(metadata))
        except Exception as exc:  # a malformed metadata blob is the client's bug
            raise HTTPException(status_code=400, detail={"error": "bad_metadata", "detail": str(exc)}) from exc
        if parsed.job_id != job_id:
            raise HTTPException(
                status_code=400,
                detail={"error": "job_mismatch", "detail": f"body says {parsed.job_id}, path says {job_id}"},
            )
        state = store.state_of(job_id)
        owing = {position for position, _reason, _dpr in store.recapture_requests(job_id)}
        if state != "captured" and parsed.seq not in owing:
            # A screen accepted here would sit on disk for the rest of the 14-day
            # cap behind a book that is already past it, and nothing would read it.
            raise HTTPException(
                status_code=409,
                detail={"error": "not_accepting_uploads", "state": state, "seq": parsed.seq},
            )
        # Bounded read: a client that streams 4 GB at an endpoint that buffers
        # first has already won by the time validation runs.
        data = await image.read(settings.max_bytes + 1)
        info = validate_image(
            data,
            declared_sha256=parsed.image_sha256,
            pixel_cap=settings.pixel_cap,
            max_bytes=settings.max_bytes,
        )
        capture = store.commit_capture(
            job_id=job_id,
            seq=parsed.seq,
            capture_id=parsed.capture_id,
            image_sha256=info.sha256,
            image_bytes=data,
            ttl_days=settings.ttl_days,
        )
        return CommitAck(
            job_id=job_id,
            seq=capture.seq,
            capture_id=capture.capture_id,
            committed=True,
            state=store.state_of(job_id),
            images_expire_at=capture.expire_at,
        )

    @app.get("/v2/jobs/{job_id}", response_model=JobStatus)
    async def job_status(job_id: str, x_ocr_protocol: str | None = Header(default=None)) -> JobStatus:
        require_protocol(x_ocr_protocol)
        return _status(store, job_id)

    @app.get("/v2/jobs/{job_id}/bundle", response_model=Bundle)
    async def job_bundle(job_id: str, x_ocr_protocol: str | None = Header(default=None)) -> Bundle:
        require_protocol(x_ocr_protocol)
        row = store.job(job_id)
        if STATES.index(row["state"]) < STATES.index("bundle_written"):
            raise HTTPException(
                status_code=409,
                detail={"error": "no_bundle", "state": row["state"]},
            )
        return Bundle(
            job_id=job_id,
            asin=row["asin"],
            state=row["state"],
            pipeline_version=row["pipeline_version"],
            captures=tuple(
                {
                    "seq": capture.seq,
                    "capture_id": capture.capture_id,
                    "image_sha256": capture.image_sha256,
                }
                for capture in store.captures(job_id, include_discarded=True)
            ),
            bundle_sha256=row["bundle_sha256"],
        )

    @app.post("/v2/jobs/{job_id}/read")
    async def read_next(
        job_id: str, x_ocr_protocol: str | None = Header(default=None)
    ) -> dict[str, object]:
        """Read every screen of this job whose reading is missing.

        Synchronous on purpose for WP1 - the request is the unit of work, and the
        tracker counts it - but the stage registers in-flight work of its own, so a
        background read in WP2 cannot be mistaken for an idle service.
        """
        require_protocol(x_ocr_protocol)
        store.job(job_id)
        unread = store.unread_captures(job_id)
        readers = app.state.readers or build_readers(settings)
        if unread:
            # A reader that cannot load contributes silence to every page, and a
            # run of N-1 readers is a different measurement than the gate was
            # computed for. So the readers are asked to read once, before the job
            # starts, and a reader that will not load stops the job with a reason
            # instead of quietly emptying the pages it could not read.
            verdicts = preflight(readers)
            unavailable = tuple(verdict for verdict in verdicts if not verdict.ok)
            if unavailable:
                readers.unload()
                reason = "; ".join(
                    f"reader {v.reader} unavailable: {v.error}" for v in unavailable
                )
                store.block(job_id, reason)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "readers_unavailable",
                        "reason": reason,
                        "readers": [
                            {"reader": v.reader, "error": v.error} for v in unavailable
                        ],
                    },
                )
        vocabulary = Vocabulary(minimum=2)
        regions_read = gaps = marks = words = words_marked = 0
        statuses: dict[str, int] = {}
        seconds = 0.0
        for capture in unread:
            data = capture.path.read_bytes()
            try:
                with app.state.work:
                    page = read_page(
                        data,
                        capture_id=capture.capture_id,
                        readers=readers,
                        vocabulary=vocabulary,
                        primary=settings.primary,
                        read_note_links=settings.read_note_links,
                    )
            except ReaderFailed as failed:
                # The readers passed their preflight, so this is a reader that
                # stopped working mid-book: stop the job rather than let the rest
                # of the pages be read by two of three readers unmarked.
                readers.unload()
                store.block(job_id, f"reader {failed.reader} unavailable: {failed.error}")
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "reader_failed",
                        "reader": failed.reader,
                        "reason": str(failed),
                        "seq": capture.seq,
                    },
                ) from failed
            for index, region in enumerate(page.regions):
                store.record_reading(
                    job_id,
                    capture.seq,
                    index,
                    region.kind,
                    region.box,
                    region.text,
                    readers=json.dumps(dict(region.readings)),
                    empty=not region.text.strip(),
                    seconds=0.0,
                )
                regions_read += 1
            for ordinal, mark in enumerate(page.marks):
                store.record_mark(
                    job_id,
                    capture.seq,
                    ordinal,
                    mark.kind,
                    mark.span,
                    mark.reason,
                    json.dumps([{"text": c.text, "readers": list(c.readers)} for c in mark.candidates]),
                )
                marks += 1
            for gap in page.gaps:
                store.record_mark(
                    job_id, capture.seq, 1000 + len(store.marks(job_id)), "gap", gap.span, gap.reason
                )
                store.record_gap(job_id, capture.seq, gap.span)
                gaps += 1
            store.set_page_status(job_id, capture.seq, page.status, page.reason)
            store.mark_read(job_id, capture.seq)
            statuses[page.status] = statuses.get(page.status, 0) + 1
            words += page.words
            words_marked += page.words_marked
            seconds += page.seconds
        if readers is not None and unread:
            readers.unload()   # the GPU is shared: nothing stays resident
        return {
            "job_id": job_id,
            "read": len(unread),
            "regions": regions_read,
            "marks": marks,
            "gaps": gaps,
            "words": words,
            "words_marked": words_marked,
            "pages": statuses,
            "seconds": round(seconds, 3),
        }

    @app.post("/v2/jobs/{job_id}/advance", response_model=JobStatus)
    async def advance(
        job_id: str,
        to: State,
        note: str = "",
        x_ocr_protocol: str | None = Header(default=None),
    ) -> JobStatus:
        require_protocol(x_ocr_protocol)
        store.advance(job_id, to, note or "advanced by the client")
        return _status(store, job_id)

    @app.post("/v2/jobs/{job_id}/finalize", response_model=JobStatus)
    async def finalize(job_id: str, x_ocr_protocol: str | None = Header(default=None)) -> JobStatus:
        """Delete the images, verify it, and only then let the job be final."""
        require_protocol(x_ocr_protocol)
        state = store.state_of(job_id)
        if state != "bundle_written":
            raise HTTPException(
                status_code=409,
                detail={"error": "not_ready_to_finalize", "state": state},
            )
        store.delete_images(job_id)
        store.advance(job_id, "images_deleted", "images deleted and verified")
        store.advance(job_id, "final", "bundle written before deletion")
        return _status(store, job_id)

    return app


def app_from_env() -> FastAPI:  # pragma: no cover - uvicorn entry point
    return create_app()
