"""Pure decode tests for the upload-finished and play-started `0x68` event
kinds (protocols/handshake.py) -- the two events a passive controller acts
on to auto-fetch and draw the job file it did not start itself."""

from __future__ import annotations

from carveracontroller.protocols.handshake import (
    EVENT_KIND_PLAY_STARTED,
    EVENT_KIND_UPLOAD_FINISHED,
    decode_play_started_event,
    decode_upload_finished_event,
)


def _upload_finished_payload(path: bytes, size: int, checksum_type: int = 0, checksum: bytes = b"") -> bytes:
    return (
        bytes([EVENT_KIND_UPLOAD_FINISHED, len(path)])
        + path
        + size.to_bytes(4, "big")
        + bytes([checksum_type])
        + checksum
    )


def test_decode_upload_finished_with_no_checksum():
    payload = _upload_finished_payload(b"/sd/job.nc", 12345)
    decoded = decode_upload_finished_event(payload)
    assert decoded is not None
    assert decoded.path == "/sd/job.nc"
    assert decoded.size == 12345
    assert decoded.checksum == b""


def test_decode_upload_finished_with_md5_checksum():
    checksum = bytes(range(16))
    payload = _upload_finished_payload(b"/sd/job.nc", 1, checksum_type=1, checksum=checksum)
    decoded = decode_upload_finished_event(payload)
    assert decoded is not None
    assert decoded.checksum == checksum


def test_decode_upload_finished_rejects_wrong_kind():
    payload = bytes([EVENT_KIND_PLAY_STARTED, 0]) + (0).to_bytes(4, "big") + bytes([0])
    assert decode_upload_finished_event(payload) is None


def test_decode_upload_finished_rejects_truncated_payload():
    assert decode_upload_finished_event(bytes([EVENT_KIND_UPLOAD_FINISHED, 5]) + b"/sd/") is None


def test_decode_play_started():
    payload = bytes([EVENT_KIND_PLAY_STARTED, len(b"/sd/job.nc")]) + b"/sd/job.nc"
    decoded = decode_play_started_event(payload)
    assert decoded is not None
    assert decoded.path == "/sd/job.nc"


def test_decode_play_started_rejects_wrong_kind():
    payload = bytes([EVENT_KIND_UPLOAD_FINISHED, 0])
    assert decode_play_started_event(payload) is None


def test_decode_play_started_rejects_truncated_payload():
    assert decode_play_started_event(bytes([EVENT_KIND_PLAY_STARTED, 5]) + b"/sd") is None
