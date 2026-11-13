"""The pack's client. Async, typed, and without a fallback.

`no fallback` is the whole point: v1's client returned empty text when the
server was unreachable (`kindle/scraper/ocr_remote.py`), which silently turned a
failed read into a blank page. Here an unreachable server is an exception, and a
screen that cannot be uploaded stops the walk.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .wire import (
    PROTOCOL_VERSION,
    Bundle,
    CommitAck,
    JobCreate,
    JobStatus,
    ProtocolMismatch,
    ScreenUpload,
)

if TYPE_CHECKING:
    from types import TracebackType

PROTOCOL_HEADER = "X-Ocr-Protocol"


class ServerError(RuntimeError):
    """The server refused, and the reason is in the message."""

    def __init__(self, status: int, detail: object) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"ocr server returned {status}: {detail}")


@dataclass
class ScreenToUpload:
    """One captured screen, as the pack has it in memory."""

    seq: int
    capture_id: str
    scrubber: int
    layout_lines: int
    dpr: int
    png: bytes
    scrubber_max: int | None = None
    footer: str = ""
    page_data_start: str | None = None
    page_data_end: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()


class OcrClient:
    """A job's worth of conversation with one server."""

    def __init__(
        self,
        base_url: str,
        *,
        job_id: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.job_id = job_id
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._owns_client = client is None

    async def __aenter__(self) -> OcrClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- plumbing ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {PROTOCOL_HEADER: PROTOCOL_VERSION}

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail: object
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        if response.status_code == 409 and isinstance(detail, dict) and detail.get("error") == "protocol_mismatch":
            raise ProtocolMismatch(detail.get("got"))
        raise ServerError(response.status_code, detail)

    # -- the job -------------------------------------------------------------

    async def create_job(self, asin: str, dpr: int, pipeline_version: str, title: str | None = None) -> JobStatus:
        response = await self._client.post(
            "/v2/jobs",
            json=JobCreate(asin=asin, dpr=dpr, pipeline_version=pipeline_version, title=title).model_dump(),
            headers=self._headers(),
        )
        self._raise_for_status(response)
        status = JobStatus.model_validate(response.json())
        self.job_id = status.job_id
        return status

    async def upload_screen(self, screen: ScreenToUpload) -> CommitAck:
        if self.job_id is None:
            raise RuntimeError("no job: call create_job first")
        metadata = ScreenUpload(
            job_id=self.job_id,
            seq=screen.seq,
            capture_id=screen.capture_id,
            scrubber=screen.scrubber,
            scrubber_max=screen.scrubber_max,
            footer=screen.footer,
            page_data_start=screen.page_data_start,
            page_data_end=screen.page_data_end,
            layout_lines=screen.layout_lines,
            dpr=screen.dpr,
            image_sha256=screen.sha256,
        )
        response = await self._client.post(
            f"/v2/jobs/{self.job_id}/screens",
            data={"metadata": json.dumps(metadata.model_dump(mode="json"))},
            files={"image": ("screen.png", screen.png, "image/png")},
            headers=self._headers(),
        )
        self._raise_for_status(response)
        return CommitAck.model_validate(response.json())

    async def status(self) -> JobStatus:
        if self.job_id is None:
            raise RuntimeError("no job: call create_job first")
        response = await self._client.get(f"/v2/jobs/{self.job_id}", headers=self._headers())
        self._raise_for_status(response)
        return JobStatus.model_validate(response.json())

    async def bundle(self) -> Bundle:
        if self.job_id is None:
            raise RuntimeError("no job: call create_job first")
        response = await self._client.get(f"/v2/jobs/{self.job_id}/bundle", headers=self._headers())
        self._raise_for_status(response)
        return Bundle.model_validate(response.json())

    async def advance(self, to: str, note: str = "") -> JobStatus:
        if self.job_id is None:
            raise RuntimeError("no job: call create_job first")
        response = await self._client.post(
            f"/v2/jobs/{self.job_id}/advance",
            params={"to": to, "note": note},
            headers=self._headers(),
        )
        self._raise_for_status(response)
        return JobStatus.model_validate(response.json())

    async def finalize(self) -> JobStatus:
        if self.job_id is None:
            raise RuntimeError("no job: call create_job first")
        response = await self._client.post(f"/v2/jobs/{self.job_id}/finalize", headers=self._headers())
        self._raise_for_status(response)
        return JobStatus.model_validate(response.json())


def read_screen_png(path: Path) -> bytes:
    """Read a screen from disk, for the pack's own tests. The service never does."""
    return Path(path).read_bytes()
