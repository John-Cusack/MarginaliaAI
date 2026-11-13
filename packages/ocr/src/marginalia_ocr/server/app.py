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
from .paths import DEFAULT_ROOT
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
        idle_for = time.monotonic() - app.state.last_request
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


def create_app(
    store: Store | None = None, settings: Settings | None = None, on_idle=None
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

    @app.middleware("http")
    async def _touch(request, call_next):
        app.state.last_request = time.monotonic()
        return await call_next(request)


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
