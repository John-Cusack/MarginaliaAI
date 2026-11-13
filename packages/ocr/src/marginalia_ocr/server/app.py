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
from dataclasses import dataclass
from datetime import UTC, datetime
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

    db_path: Path = Path("/var/lib/marginalia-ocr/jobs.sqlite3")
    page_store: Path = Path("/var/lib/marginalia-ocr/pages")
    pixel_cap: int = DEFAULT_PIXEL_CAP
    max_bytes: int = DEFAULT_MAX_BYTES
    ttl_days: int = IMAGE_TTL_DAYS

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        root = Path(env.get("MARGINALIA_OCR_ROOT", "/var/lib/marginalia-ocr"))
        return cls(
            db_path=Path(env.get("MARGINALIA_OCR_DB", root / "jobs.sqlite3")),
            page_store=Path(env.get("MARGINALIA_OCR_PAGES", root / "pages")),
            pixel_cap=int(env.get("MARGINALIA_OCR_PIXEL_CAP", DEFAULT_PIXEL_CAP)),
            max_bytes=int(env.get("MARGINALIA_OCR_MAX_BYTES", DEFAULT_MAX_BYTES)),
            ttl_days=int(env.get("MARGINALIA_OCR_TTL_DAYS", IMAGE_TTL_DAYS)),
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
        images_expire_at=datetime.now(UTC),
        recapture=tuple(
            RecaptureRequest(position=position, reason=reason, dpr=dpr)
            for position, reason, dpr in store.recapture_requests(job_id)
        ),
        error=row["error"],
    )


def create_app(store: Store | None = None, settings: Settings | None = None) -> FastAPI:
    settings = (settings or Settings.from_env()).validate()
    store = store or Store(settings.db_path, settings.page_store)
    app = FastAPI(title="marginalia-ai-ocr", version=PROTOCOL_VERSION)
    app.state.store = store
    app.state.settings = settings

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
        data = await image.read()
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
