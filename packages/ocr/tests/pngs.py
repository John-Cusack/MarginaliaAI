"""Tiny PNG builders shared by the service tests.

Not a fixture: several test modules need the same bytes, and an animated PNG or
an over-cap header is easier to state as a function than as a fixture.
"""

from __future__ import annotations

import io
import struct

from PIL import Image

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_bytes(width: int = 40, height: int = 30, colour: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def chunk(name: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + name + body + b"\x00\x00\x00\x00"


def header_only(width: int, height: int) -> bytes:
    """A structurally valid PNG with declared dimensions and no pixel data."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")
