"""Wire protocol v2: the shape a pack and a server must agree on.

The version field is not documentation. v1's client fell back to an empty read
when the server disagreed with it, which turned a protocol error into a blank
page; v2 rejects the mismatch instead, and this file pins that.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from marginalia_ocr.wire import (
    PROTOCOL_VERSION,
    STATES,
    Bundle,
    CommitAck,
    JobCreate,
    JobStatus,
    ProtocolMismatch,
    RecaptureRequest,
    ScreenUpload,
    check_protocol,
)
from pydantic import ValidationError

SHA = "a" * 64


def test_the_protocol_version_is_two_and_the_states_are_the_designs():
    assert PROTOCOL_VERSION == "2"
    assert STATES[0] == "captured" and STATES[-1] == "final"
    assert "images_deleted" in STATES and STATES.index("images_deleted") < STATES.index("final")


def test_a_job_create_round_trips_through_json():
    job = JobCreate(asin="B004774792", dpr=3, pipeline_version="wp1")
    again = JobCreate.model_validate(json.loads(job.model_dump_json()))
    assert again == job
    assert again.protocol == PROTOCOL_VERSION


def test_a_screen_upload_carries_what_binding_needs():
    """The metadata is the evidence that the image belongs to a render."""
    upload = ScreenUpload(
        job_id="j1",
        seq=7,
        capture_id="c1",
        scrubber=2614,
        scrubber_max=1933298,
        footer="Location 27 of 16166",
        page_data_start="27",
        page_data_end="28",
        layout_lines=44,
        dpr=3,
        image_sha256=SHA,
    )
    again = ScreenUpload.model_validate(json.loads(upload.model_dump_json()))
    assert again == upload
    assert again.page_data_start == "27" and again.layout_lines == 44


def test_a_mismatched_protocol_is_rejected_not_negotiated():
    with pytest.raises(ProtocolMismatch) as caught:
        check_protocol("1")
    assert caught.value.got == "1" and caught.value.want == "2"
    assert check_protocol("2") == "2"
    with pytest.raises(ProtocolMismatch):
        check_protocol(None)


def test_every_message_carries_the_protocol_it_was_written_under():
    moment = datetime.now(UTC)
    for message in (
        JobCreate(asin="B0", dpr=2, pipeline_version="v"),
        ScreenUpload(
            job_id="j", seq=0, capture_id="c", scrubber=1, layout_lines=0, dpr=2, image_sha256=SHA
        ),
        CommitAck(
            job_id="j", seq=0, capture_id="c", committed=True, state="captured", images_expire_at=moment
        ),
        JobStatus(job_id="j", asin="B0", state="captured", captures_committed=0),
        RecaptureRequest(position=3, reason="expired", dpr=3),
        Bundle(job_id="j", asin="B0", state="final", pipeline_version="v"),
    ):
        assert message.protocol == PROTOCOL_VERSION


def test_an_unknown_field_is_refused():
    """A client and a server that silently ignore each other's fields drift."""
    with pytest.raises(ValidationError):
        ScreenUpload(
            job_id="j",
            seq=0,
            capture_id="c",
            scrubber=1,
            layout_lines=0,
            dpr=1,
            image_sha256=SHA,
            surprise="ignored?",
        )


def test_a_short_sha_is_not_an_image_hash():
    with pytest.raises(ValidationError):
        ScreenUpload(
            job_id="j", seq=0, capture_id="c", scrubber=1, layout_lines=0, dpr=1, image_sha256="abc"
        )
