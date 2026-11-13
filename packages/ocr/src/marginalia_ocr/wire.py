"""Wire protocol v2: job-oriented, one page per upload, structured results.

Version 1 of this protocol (the Kindle pack's `kindle/ocr_server.py`) carried a
whole page of text back and fell back to an empty read when the server was
unhappy. Neither survives here: v2 moves one screen at a time, every upload
carries the metadata the binding check needs, and there is no fallback — a screen
that is not read is a gap, and a gap is reported.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# `datetime` stays a runtime import (noqa TC003): pydantic builds the models at
# import time and cannot resolve an annotation-only name.  # noqa: TC003

#: Bumped whenever a field's meaning changes. The server rejects a client that
#: disagrees rather than guessing: a page stored under the wrong rules is worse
#: than a page not stored.
PROTOCOL_VERSION = "2"

STATES = (
    "captured",
    "read",
    "checked",
    "assembled",
    "audited",
    "bundle_written",
    "images_deleted",
    "final",
)
State = Literal[
    "captured", "read", "checked", "assembled", "audited", "bundle_written", "images_deleted", "final"
]

#: The bundle is derived-only and kept permanently, so its shape is a contract.
BUNDLE_SCHEMA = 1


class ProtocolMismatch(ValueError):
    """The client and the server do not speak the same version."""

    def __init__(self, got: str | None, want: str = PROTOCOL_VERSION) -> None:
        self.got = got
        self.want = want
        super().__init__(f"protocol {got!r} is not {want!r}")


def check_protocol(value: str | None, want: str = PROTOCOL_VERSION) -> str:
    """Reject a mismatched protocol, or return it."""
    if value != want:
        raise ProtocolMismatch(value, want)
    return want


class Wire(BaseModel):
    """Every message carries the protocol it was written under."""

    model_config = ConfigDict(extra="forbid")

    protocol: str = Field(default=PROTOCOL_VERSION)


class JobCreate(Wire):
    """Start a job for one book: the pack's unit of work."""

    asin: str
    dpr: int = Field(ge=1, le=4)
    pipeline_version: str
    title: str | None = None


class ScreenUpload(Wire):
    """One screen, with everything the server needs to bind it to a render.

    The metadata is not decoration: `scrubber`, the `page_data` range and the
    footer are what prove the screenshot belongs to that render response, and
    `image_sha256` is what proves the bytes arrived whole.
    """

    job_id: str
    seq: int = Field(ge=0)
    capture_id: str
    scrubber: int
    scrubber_max: int | None = None
    footer: str = ""
    page_data_start: str | None = None
    page_data_end: str | None = None
    layout_lines: int = Field(ge=0)
    dpr: int = Field(ge=1, le=4)
    image_sha256: str = Field(min_length=64, max_length=64)


class CommitAck(Wire):
    """What the pack may treat as durable progress."""

    job_id: str
    seq: int
    capture_id: str
    committed: bool
    state: State
    images_expire_at: datetime


class RecaptureRequest(Wire):
    """A position whose image expired or proved unreadable, to be taken again."""

    position: int
    reason: str
    dpr: int = Field(ge=1, le=4)


class JobStatus(Wire):
    job_id: str
    asin: str
    state: State
    captures_committed: int
    pages_marked: int = 0
    pages_gapped: int = 0
    last_committed_position: int | None = None
    images_expire_at: datetime | None = None
    recapture: tuple[RecaptureRequest, ...] = ()
    error: str | None = None


class Bundle(Wire):
    """Derived-only, kept permanently. Replaces `extracted/{asin}.txt`."""

    schema_version: int = BUNDLE_SCHEMA
    job_id: str
    asin: str
    state: State
    pipeline_version: str
    final_text: str | None = None
    captures: tuple[dict[str, object], ...] = ()
    bundle_sha256: str | None = None
    audit_sample: tuple[int, ...] = ()
