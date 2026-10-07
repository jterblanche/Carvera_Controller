"""Wire format of the job-start sync: the features byte in the hello and
its ack, the play-started event's size and checksum, the job-start event
(0x68 kind 8) and the ready frame (0x6C)."""

from __future__ import annotations

import hashlib

from carveracontroller.machine.hello import HelloNegotiator
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import PTYPE_HELLO, validate_packet_data
from carveracontroller.protocols.handshake import (
    EVENT_KIND_JOB_START,
    EVENT_KIND_PLAY_STARTED,
    HELLO_ACCEPTED,
    HELLO_FEATURE_JOB_START_WAIT,
    JOB_START_CANCELLED,
    JOB_START_HASHING,
    JOB_START_REASON_STARTER_LEFT,
    JOB_START_REASON_WAITING,
    JOB_START_WAITING,
    HelloAck,
    decode_hello_ack,
    decode_job_start_event,
    decode_play_started_event,
)
from carveracontroller.protocols.makera import (
    HELLO_PROTOCOL_VERSION,
    MakeraProtocol,
    encode_hello,
    encode_job_start_ready,
)
from carveracontroller.protocols.messages import MessageKind

MD5 = hashlib.md5(b"G0 X0\n").digest()


def _parse(frame):
    assert frame[:2] == b"\x86\x68"
    assert frame[-2:] == b"\x55\xaa"
    parsed = validate_packet_data(frame[2:-2])
    assert parsed is not None
    return parsed


def _file_part(path=b"/sd/gcodes/part.nc", size=6, md5=MD5):
    part = bytes([len(path)]) + path + size.to_bytes(4, "big")
    if md5 is None:
        return part + bytes([0])
    return part + bytes([1]) + md5


def job_start_payload(
    start_id=7,
    phase=JOB_START_WAITING,
    reason=JOB_START_REASON_WAITING,
    seconds_left=30,
    starter_id=0x1111,
    not_ready=(0x2222,),
    path=b"/sd/gcodes/part.nc",
    size=6,
    md5=MD5,
):
    payload = bytes([EVENT_KIND_JOB_START]) + _file_part(path, size, md5)
    payload += start_id.to_bytes(2, "big") + bytes([phase, reason, seconds_left])
    payload += starter_id.to_bytes(8, "big") + bytes([len(not_ready)])
    for client_id in not_ready:
        payload += client_id.to_bytes(8, "big")
    return payload


# -- hello and its ack --------------------------------------------------------


def test_hello_ack_of_four_bytes_carries_the_features_byte():
    ack = decode_hello_ack(bytes([1, HELLO_ACCEPTED, 0, HELLO_FEATURE_JOB_START_WAIT]))
    assert ack is not None
    assert ack.features == HELLO_FEATURE_JOB_START_WAIT


def test_hello_ack_of_three_bytes_has_no_features():
    """Firmware without the job-start wait sends three bytes."""
    ack = decode_hello_ack(bytes([1, HELLO_ACCEPTED, 1]))
    assert ack is not None
    assert ack.mode == 1
    assert ack.features == 0


def test_encode_hello_appends_the_features_byte():
    parsed = _parse(encode_hello(0x0102030405060708, b"PC", 1, features=HELLO_FEATURE_JOB_START_WAIT))
    assert parsed.ptype == PTYPE_HELLO
    name_len = parsed.payload[9]
    assert parsed.payload[10 + name_len] == 1  # link
    assert parsed.payload[11 + name_len] == HELLO_FEATURE_JOB_START_WAIT
    assert len(parsed.payload) == 12 + name_len


def test_encode_hello_without_features_keeps_the_old_layout():
    parsed = _parse(encode_hello(1, b"PC", 0))
    assert len(parsed.payload) == 11 + 2


def test_the_hello_this_controller_sends_says_it_takes_part():
    negotiator = HelloNegotiator(identity=ControllerIdentity(id=5, name="Demo"), link=0)
    parsed = _parse(negotiator.on_valid_frame(0.0))
    assert parsed.payload[-1] == HELLO_FEATURE_JOB_START_WAIT


def test_negotiator_records_whether_the_machine_holds_starts():
    negotiator = HelloNegotiator(identity=ControllerIdentity(id=5, name="Demo"), link=0)
    negotiator.on_valid_frame(0.0)
    assert negotiator.machine_holds_starts is False
    negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_ACCEPTED, 0, HELLO_FEATURE_JOB_START_WAIT))
    assert negotiator.machine_holds_starts is True


def test_negotiator_with_a_three_byte_ack_never_expects_a_hold():
    negotiator = HelloNegotiator(identity=ControllerIdentity(id=5, name="Demo"), link=0)
    negotiator.on_valid_frame(0.0)
    negotiator.on_hello_ack(decode_hello_ack(bytes([1, HELLO_ACCEPTED, 0])))
    assert negotiator.identified
    assert negotiator.machine_holds_starts is False


# -- ready frame --------------------------------------------------------------


def test_ready_frame_carries_the_start_id():
    parsed = _parse(encode_job_start_ready(0x1234))
    assert parsed.ptype == 0x6C
    assert parsed.payload == bytes([0x12, 0x34])


# -- play-started -------------------------------------------------------------


def test_play_started_from_old_firmware_has_unknown_size_and_no_checksum():
    payload = bytes([EVENT_KIND_PLAY_STARTED, len(b"/sd/job.nc")]) + b"/sd/job.nc"
    decoded = decode_play_started_event(payload)
    assert decoded is not None
    assert decoded.path == "/sd/job.nc"
    assert decoded.size is None
    assert decoded.checksum == b""


def test_play_started_with_size_and_md5():
    payload = bytes([EVENT_KIND_PLAY_STARTED]) + _file_part(b"/sd/job.nc", 1234, MD5)
    decoded = decode_play_started_event(payload)
    assert decoded is not None
    assert decoded.path == "/sd/job.nc"
    assert decoded.size == 1234
    assert decoded.checksum == MD5


def test_play_started_with_size_and_no_checksum():
    payload = bytes([EVENT_KIND_PLAY_STARTED]) + _file_part(b"/sd/job.nc", 1234, None)
    decoded = decode_play_started_event(payload)
    assert decoded is not None
    assert decoded.size == 1234
    assert decoded.checksum == b""


def test_play_started_with_a_cut_short_digest_is_dropped():
    payload = bytes([EVENT_KIND_PLAY_STARTED]) + _file_part(b"/sd/job.nc", 1234, MD5)[:-3]
    assert decode_play_started_event(payload) is None


# -- job-start event ----------------------------------------------------------


def test_job_start_event_full_layout():
    event = decode_job_start_event(job_start_payload(not_ready=(0x2222, 0x3333)))
    assert event is not None
    assert event.path == "/sd/gcodes/part.nc"
    assert event.size == 6
    assert event.checksum == MD5
    assert event.start_id == 7
    assert event.phase == JOB_START_WAITING
    assert event.reason == JOB_START_REASON_WAITING
    assert event.seconds_left == 30
    assert event.starter_id == 0x1111
    assert event.not_ready_ids == (0x2222, 0x3333)


def test_job_start_event_without_a_checksum():
    event = decode_job_start_event(job_start_payload(md5=None, not_ready=()))
    assert event is not None
    assert event.checksum == b""
    assert event.not_ready_ids == ()


def test_job_start_event_cancelled():
    event = decode_job_start_event(
        job_start_payload(phase=JOB_START_CANCELLED, reason=JOB_START_REASON_STARTER_LEFT, seconds_left=0)
    )
    assert event is not None
    assert event.phase == JOB_START_CANCELLED
    assert event.reason == JOB_START_REASON_STARTER_LEFT


def test_job_start_event_hashing_carries_the_size_and_no_checksum():
    """While the machine computes the file's MD5 at a held start, its
    events say so (phase 3), with the file's size and no checksum yet."""
    event = decode_job_start_event(job_start_payload(phase=JOB_START_HASHING, md5=None, seconds_left=0))
    assert event is not None
    assert event.phase == JOB_START_HASHING == 3
    assert event.size == 6
    assert event.checksum == b""
    assert event.seconds_left == 0
    assert event.not_ready_ids == (0x2222,)


def test_job_start_event_with_start_id_zero_is_dropped():
    assert decode_job_start_event(job_start_payload(start_id=0)) is None


def test_job_start_event_cut_short_is_dropped():
    payload = job_start_payload(not_ready=(0x2222, 0x3333))
    for cut in (1, 8, 9, 20):
        assert decode_job_start_event(payload[:-cut]) is None


def test_job_start_decoder_ignores_other_kinds():
    payload = bytes([EVENT_KIND_PLAY_STARTED]) + job_start_payload()[1:]
    assert decode_job_start_event(payload) is None


def test_job_start_event_arrives_as_an_event_message():
    from carveracontroller.protocols.framing import PTYPE_EVENT, build_frame

    messages = MakeraProtocol().feed(build_frame(PTYPE_EVENT, job_start_payload()))
    assert [m.kind for m in messages] == [MessageKind.EVENT]
    assert decode_job_start_event(messages[0].payload) is not None
