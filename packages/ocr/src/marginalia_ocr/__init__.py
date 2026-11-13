"""The verified-OCR service and its client.

Two halves, one dist: the Kindle pack imports the client and the wire types, and
the super server runs the service. Neither half has a fallback path — a page that
cannot be read is marked, never guessed at.
"""

from .wire import (
    BUNDLE_SCHEMA,
    PROTOCOL_VERSION,
    CommitAck,
    JobCreate,
    JobStatus,
    ProtocolMismatch,
    RecaptureRequest,
    ScreenUpload,
    check_protocol,
)

__all__ = [
    "BUNDLE_SCHEMA",
    "PROTOCOL_VERSION",
    "CommitAck",
    "JobCreate",
    "JobStatus",
    "ProtocolMismatch",
    "RecaptureRequest",
    "ScreenUpload",
    "check_protocol",
]
