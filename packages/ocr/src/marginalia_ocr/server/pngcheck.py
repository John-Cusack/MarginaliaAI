"""PNG validation: bounded, single-frame, within the pixel cap.

Every image that reaches the page store passes through here first. The pack sends
what the browser rendered; the server accepts only what it can reason about. A
file that fails any bound is refused with a reason, never stored and never read:
an image the server cannot describe is not evidence about a page.
"""

from __future__ import annotations

import hashlib
import io
import struct
from dataclasses import dataclass

#: v1 rejected anything over 4,000,000 px, which is smaller than a 3x screen of
#: 1280x780 (~9.0 M px). The cap has to clear 3x, so this is the floor, not a
#: preference: a config below it cannot serve the verified path at all.
MIN_PIXEL_CAP = 9_500_000
DEFAULT_PIXEL_CAP = 12_000_000
#: A 3x PNG of a text page is well under this; a decompression bomb is not.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PngRejected(ValueError):
    """The bytes are not an image this service will accept."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class PngInfo:
    width: int
    height: int
    bytes_uploaded: int
    sha256: str

    @property
    def pixels(self) -> int:
        return self.width * self.height


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Walk the PNG chunk list, refusing anything structurally impossible."""
    chunks: list[tuple[bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    while offset < len(data):
        if offset + 8 > len(data):
            raise PngRejected("truncated", "chunk header runs past the end")
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        name = data[offset + 4 : offset + 8]
        body_start = offset + 8
        body_end = body_start + length
        if body_end + 4 > len(data):
            raise PngRejected("truncated", f"chunk {name!r} runs past the end")
        chunks.append((name, data[body_start:body_end]))
        offset = body_end + 4  # skip the CRC
        if name == b"IEND":
            break
    return chunks


def validate_image(
    data: bytes,
    *,
    declared_sha256: str | None = None,
    pixel_cap: int = DEFAULT_PIXEL_CAP,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> PngInfo:
    """Validate one uploaded screen and describe it.

    Bounded (bytes), single frame (an APNG is a sequence, and a screen is not),
    inside the pixel cap, structurally complete, and - when the client declares
    one - exactly the bytes it hashed.
    """
    if pixel_cap < MIN_PIXEL_CAP:
        raise PngRejected("bad configuration", f"pixel cap {pixel_cap} is under {MIN_PIXEL_CAP}")
    if not data:
        raise PngRejected("empty upload")
    if len(data) > max_bytes:
        raise PngRejected("too large", f"{len(data)} bytes over the {max_bytes} limit")
    if not data.startswith(PNG_SIGNATURE):
        raise PngRejected("not a png", "the signature is missing")

    chunks = _chunks(data)
    if not chunks:
        raise PngRejected("truncated", "no chunks")
    if chunks[-1][0] != b"IEND":
        raise PngRejected("truncated", "no IEND chunk")
    for name, _body in chunks:
        if name == b"acTL":
            raise PngRejected("animated png", "a screen is one frame, not a sequence")
        if name == b"fdAT":
            raise PngRejected("animated png", "frame data has no place in a screen")

    header = next((body for name, body in chunks if name == b"IHDR"), None)
    if header is None or len(header) < 8:
        raise PngRejected("no header", "IHDR is missing or short")
    width, height = struct.unpack(">II", header[:8])
    if width == 0 or height == 0:
        raise PngRejected("zero dimension", f"{width}x{height}")
    pixels = width * height
    if pixels > pixel_cap:
        raise PngRejected("over the pixel cap", f"{pixels} px over the {pixel_cap} cap")

    digest = hashlib.sha256(data).hexdigest()
    if declared_sha256 is not None and digest != declared_sha256:
        raise PngRejected("sha256 mismatch", f"declared {declared_sha256[:12]}, received {digest[:12]}")
    return PngInfo(width=width, height=height, bytes_uploaded=len(data), sha256=digest)


def open_image(data: bytes):
    """Decode a validated screen, in memory only.

    Nothing in this service writes a derived crop to disk: the page image is the
    only image that touches storage, and only for as long as its job lives.
    """
    from PIL import Image

    return Image.open(io.BytesIO(data))
