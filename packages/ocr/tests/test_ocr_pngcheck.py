"""PNG validation: bounded, single frame, inside the pixel cap.

Every bound here exists because of a way a capture can go wrong: a truncated
upload over a flaky link, a decompression bomb, an animated "screenshot", bytes
that are not the ones the client hashed. The server stores only what it can
describe, and the pixel cap has to clear a 3x screen or the verified path cannot
run at all.
"""

from __future__ import annotations

import hashlib
import struct

import pytest
from marginalia_ocr.server.pngcheck import (
    DEFAULT_PIXEL_CAP,
    MIN_PIXEL_CAP,
    PNG_SIGNATURE,
    PngRejected,
    open_image,
    validate_image,
)
from pngs import chunk, header_only, png_bytes


def test_a_real_png_is_accepted_and_described():
    data = png_bytes(1280, 780)
    info = validate_image(data, declared_sha256=hashlib.sha256(data).hexdigest())
    assert (info.width, info.height) == (1280, 780)
    assert info.pixels == 1280 * 780
    assert info.bytes_uploaded == len(data)
    with open_image(data) as image:
        assert image.size == (1280, 780)


def test_the_cap_clears_a_three_x_screen():
    """A 3x 1280x780 screen is ~9.0 M px; a cap below that cannot serve v2."""
    assert MIN_PIXEL_CAP >= 9_000_000
    assert DEFAULT_PIXEL_CAP >= MIN_PIXEL_CAP
    info = validate_image(png_bytes(1280, 780))
    assert info.pixels < DEFAULT_PIXEL_CAP


def test_a_page_over_the_pixel_cap_is_refused():
    """15 M px is a decompression bomb, not a 3x screen."""
    with pytest.raises(PngRejected) as caught:
        validate_image(header_only(5000, 3000))
    assert caught.value.reason == "over the pixel cap"


def test_a_config_below_the_floor_is_a_configuration_error():
    with pytest.raises(PngRejected) as caught:
        validate_image(header_only(10, 10), pixel_cap=4_000_000)
    assert caught.value.reason == "bad configuration"


def test_an_upload_over_the_byte_limit_is_refused_before_it_is_parsed():
    with pytest.raises(PngRejected) as caught:
        validate_image(png_bytes(200, 200), max_bytes=64)
    assert caught.value.reason == "too large"


def test_bytes_that_are_not_a_png_are_refused():
    with pytest.raises(PngRejected) as caught:
        validate_image(b"<html>not an image</html>")
    assert caught.value.reason == "not a png"
    with pytest.raises(PngRejected):
        validate_image(b"")


def test_a_truncated_upload_is_refused():
    data = png_bytes()
    with pytest.raises(PngRejected) as caught:
        validate_image(data[: len(data) // 2])
    assert caught.value.reason == "truncated"
    with pytest.raises(PngRejected):
        validate_image(PNG_SIGNATURE)


def test_an_animated_png_is_not_a_screen():
    """A screen is one frame. An APNG is a sequence, and a walk cannot bind one."""
    data = png_bytes()
    animated = data[:33] + chunk(b"acTL", struct.pack(">II", 2, 0)) + data[33:]
    with pytest.raises(PngRejected) as caught:
        validate_image(animated)
    assert caught.value.reason == "animated png"


def test_a_sha_mismatch_is_refused():
    data = png_bytes()
    with pytest.raises(PngRejected) as caught:
        validate_image(data, declared_sha256="b" * 64)
    assert caught.value.reason == "sha256 mismatch"
    assert hashlib.sha256(data).hexdigest()[:12] in caught.value.detail
