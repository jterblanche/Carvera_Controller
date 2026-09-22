import pytest

from carveracontroller.protocols.framing import (
    PTYPE_CLIENT_LIST_REPLY,
    PTYPE_EVENT,
    PTYPE_FILE_MD5,
    PTYPE_HEARTBEAT,
    PTYPE_HELLO_ACK,
    PTYPE_LOAD_ERROR,
    PTYPE_LOAD_FINISH,
    PTYPE_LOAD_INFO,
    PTYPE_NORMAL_INFO,
    PTYPE_PUBLISHED_LINE,
    PTYPE_STATUS_RES,
    build_frame,
    validate_packet_data,
)
from carveracontroller.protocols.handshake import decode_client_list, decode_hello_ack
from carveracontroller.protocols.makera import (
    MakeraProtocol,
    encode_automatic_command,
    encode_client_list_request,
    encode_heartbeat,
    encode_hello,
)
from carveracontroller.protocols.messages import MessageKind
from carveracontroller.protocols.session import ProtocolSession


def test_encode_realtime_and_command():
    proto = MakeraProtocol()
    frame = proto.encode_realtime(ord("?"))
    assert frame[:2] == b"\x86\x68"
    assert frame[-2:] == b"\x55\xaa"

    cmd = proto.encode_command(b"version\n")
    assert cmd[:2] == b"\x86\x68"
    # Trailing newlines must be stripped from CTRL_MULTI payloads (OEM behavior).
    assert b"version\n" not in cmd
    assert b"version" in cmd

    baud = proto.encode_command(b"baud 230400\n")
    assert b"230400\n" not in baud
    assert b"baud 230400" in baud

    file_cmd = proto.encode_file_command(b"upload /sd/a.nc")
    assert file_cmd[:2] == b"\x86\x68"
    # File-start frames keep the trailing newline.
    assert b"upload /sd/a.nc\n" in file_cmd


def test_feed_status_frame():
    proto = MakeraProtocol()
    payload = b"<Idle|MPos:1,2,3>"
    frame = build_frame(PTYPE_STATUS_RES, payload)
    msgs = proto.feed(frame)
    assert len(msgs) == 1
    assert msgs[0].kind == MessageKind.LINE
    assert msgs[0].text == payload.decode()


def test_feed_normal_info_and_load():
    proto = MakeraProtocol()
    msgs = proto.feed(build_frame(PTYPE_NORMAL_INFO, b"ok\r\n"))
    assert msgs[0].kind == MessageKind.LINE
    assert "ok" in msgs[0].text

    msgs = proto.feed(build_frame(PTYPE_LOAD_INFO, b"file.nc\n"))
    assert msgs[0].kind == MessageKind.LOAD_CHUNK
    assert "file.nc" in msgs[0].text


def test_feed_normal_info_buffers_until_newline():
    proto = MakeraProtocol()
    assert proto.feed(build_frame(PTYPE_NORMAL_INFO, b"hel")) == []
    assert proto.feed(build_frame(PTYPE_NORMAL_INFO, b"lo")) == []

    msgs = proto.feed(build_frame(PTYPE_NORMAL_INFO, b" world\r\n"))
    assert len(msgs) == 1
    assert msgs[0].kind == MessageKind.LINE
    assert msgs[0].text == "hello world\r"


def test_feed_normal_info_splits_on_embedded_newlines():
    proto = MakeraProtocol()
    msgs = proto.feed(build_frame(PTYPE_NORMAL_INFO, b"one\ntwo\npartial"))
    assert [m.text for m in msgs] == ["one", "two"]

    msgs = proto.feed(build_frame(PTYPE_NORMAL_INFO, b" line\n"))
    assert len(msgs) == 1
    assert msgs[0].text == "partial line"


def test_feed_normal_info_reset_discards_partial_line():
    proto = MakeraProtocol()
    assert proto.feed(build_frame(PTYPE_NORMAL_INFO, b"leftover")) == []
    proto.reset()
    msgs = proto.feed(build_frame(PTYPE_NORMAL_INFO, b"fresh\n"))
    assert len(msgs) == 1
    assert msgs[0].text == "fresh"


def test_session_emits_one_mdi_line_for_buffered_normal_info():
    session = ProtocolSession()
    session.select("makera")
    assert session.feed(build_frame(PTYPE_NORMAL_INFO, b"Build version: ")) == []
    assert session.feed(build_frame(PTYPE_NORMAL_INFO, b"edge-")) == []
    msgs = session.feed(build_frame(PTYPE_NORMAL_INFO, b"123\r\n"))
    assert len(msgs) == 1
    assert msgs[0].kind == MessageKind.LINE
    assert msgs[0].text == "Build version: edge-123"


def test_feed_status_is_not_held_by_normal_info_buffer():
    proto = MakeraProtocol()
    assert proto.feed(build_frame(PTYPE_NORMAL_INFO, b"partial")) == []
    msgs = proto.feed(build_frame(PTYPE_STATUS_RES, b"<Idle>"))
    assert len(msgs) == 1
    assert msgs[0].text == "<Idle>"
    msgs = proto.feed(build_frame(PTYPE_NORMAL_INFO, b" line\n"))
    assert len(msgs) == 1
    assert msgs[0].text == "partial line"


def test_feed_load_finish_and_error():
    proto = MakeraProtocol()
    assert proto.feed(build_frame(PTYPE_LOAD_FINISH, b"done"))[0].kind == MessageKind.LOAD_EOF
    assert proto.feed(build_frame(PTYPE_LOAD_ERROR, b"err"))[0].kind == MessageKind.LOAD_ERROR


def test_feed_rejects_bad_footer():
    proto = MakeraProtocol()
    frame = bytearray(build_frame(PTYPE_STATUS_RES, b"<Idle>"))
    frame[-1] = 0x00
    assert proto.feed(bytes(frame)) == []


def test_feed_byte_at_a_time():
    proto = MakeraProtocol()
    frame = build_frame(PTYPE_STATUS_RES, b"<Run>")
    msgs = []
    for b in frame:
        msgs.extend(proto.feed(bytes([b])))
    assert len(msgs) == 1
    assert msgs[0].text == "<Run>"


def test_file_transfer_frames_ignored_on_control_channel():
    proto = MakeraProtocol()
    frame = build_frame(PTYPE_FILE_MD5, b"3bc28b19cfca32e413fd9029000117a3")
    assert proto.feed(frame) == []


def test_frame_confirmed_false_until_a_valid_frame_arrives():
    proto = MakeraProtocol()
    assert proto.frame_confirmed is False

    # A bad-footer frame is not a valid frame: must not confirm.
    bad = bytearray(build_frame(PTYPE_STATUS_RES, b"<Idle>"))
    bad[-1] = 0x00
    proto.feed(bytes(bad))
    assert proto.frame_confirmed is False

    proto.feed(build_frame(PTYPE_STATUS_RES, b"<Idle>"))
    assert proto.frame_confirmed is True


def test_frame_confirmed_set_by_any_valid_type_not_just_status():
    proto = MakeraProtocol()
    proto.feed(build_frame(PTYPE_HELLO_ACK, bytes([1, 0, 0])))
    assert proto.frame_confirmed is True


def test_frame_confirmed_persists_across_reset_parser_but_not_full_reset():
    proto = MakeraProtocol()
    proto.feed(build_frame(PTYPE_STATUS_RES, b"<Idle>"))
    assert proto.frame_confirmed is True

    # reset() clears RX parser state (used for mid-session error recovery)
    # but must not un-confirm a link that already proved itself framed.
    proto.reset()
    assert proto.frame_confirmed is True


def test_hello_ack_dispatched_before_unknown_type_fallback():
    proto = MakeraProtocol()
    frame = build_frame(PTYPE_HELLO_ACK, bytes([1, 0, 1]))

    msgs = proto.feed(frame)

    assert len(msgs) == 1
    assert msgs[0].kind == MessageKind.HELLO_ACK
    ack = decode_hello_ack(msgs[0].payload)
    assert ack is not None
    assert (ack.protocol_version, ack.result, ack.mode) == (1, 0, 1)


def test_client_list_reply_dispatched():
    entry = (1).to_bytes(8, "big") + bytes([1]) + b"A" + bytes([0, 1])
    payload = bytes([1]) + entry
    frame = build_frame(PTYPE_CLIENT_LIST_REPLY, payload)

    msgs = MakeraProtocol().feed(frame)

    assert len(msgs) == 1
    assert msgs[0].kind == MessageKind.CLIENT_LIST
    entries = decode_client_list(msgs[0].payload)
    assert len(entries) == 1
    assert entries[0].name == "A"
    assert entries[0].has_control is True


def test_encode_hello_is_a_well_formed_frame():
    frame = encode_hello(0x0102030405060708, b"Office PC", 0)

    assert frame[:2] == b"\x86\x68"
    assert frame[-2:] == b"\x55\xaa"
    parsed = validate_packet_data(frame[2:-2])
    assert parsed is not None
    assert parsed.ptype == 0x60  # PTYPE_HELLO
    version, controller_id, name_len = parsed.payload[0], parsed.payload[1:9], parsed.payload[9]
    assert version == 1
    assert int.from_bytes(controller_id, "big") == 0x0102030405060708
    assert name_len == len(b"Office PC")
    assert parsed.payload[10 : 10 + name_len] == b"Office PC"
    assert parsed.payload[10 + name_len] == 0  # link = WiFi


def test_encode_hello_rejects_an_overlong_name():
    with pytest.raises(ValueError):
        encode_hello(1, b"x" * 32, 0)


def test_encode_client_list_request_is_a_bare_frame():
    frame = encode_client_list_request()
    assert frame[:2] == b"\x86\x68"
    assert frame[-2:] == b"\x55\xaa"


def test_encode_automatic_command_matches_ordinary_channel_normalisation():
    proto = MakeraProtocol()

    ordinary = proto.encode_command(b"version\n")
    wrapped = encode_automatic_command(0, b"version\n")
    # Same normalised text ("version", newline stripped), different type byte.
    assert b"version" in ordinary
    assert b"version" in wrapped
    assert ordinary[4] != wrapped[4]  # type byte differs (0xA2 vs 0x6B)

    file_ordinary = proto.encode_file_command(b"download /sd/a.nc")
    file_wrapped = encode_automatic_command(1, b"download /sd/a.nc")
    assert b"download /sd/a.nc\n" in file_ordinary
    assert b"download /sd/a.nc\n" in file_wrapped


def test_encode_heartbeat_is_an_empty_payload_frame():
    frame = encode_heartbeat()
    assert frame[:2] == b"\x86\x68"
    assert frame[-2:] == b"\x55\xaa"
    parsed = validate_packet_data(frame[2:-2])
    assert parsed is not None
    assert parsed.ptype == PTYPE_HEARTBEAT
    assert parsed.payload == b""


def _published_line_payload(source_id, name, text, more=False):
    return source_id.to_bytes(8, "big") + bytes([len(name)]) + name + bytes([1 if more else 0]) + text


def test_published_line_dispatched_as_a_single_fragment():
    payload = _published_line_payload(0x0102030405060708, b"Shop PC", b"version")
    msgs = MakeraProtocol().feed(build_frame(PTYPE_PUBLISHED_LINE, payload))

    assert len(msgs) == 1
    assert msgs[0].kind == MessageKind.PUBLISHED_LINE
    assert msgs[0].source_id == 0x0102030405060708
    assert msgs[0].source_name == "Shop PC"
    assert msgs[0].text == "version"


def test_published_line_reassembled_across_more_fragments():
    proto = MakeraProtocol()
    first = _published_line_payload(1, b"Shop PC", b"hello ", more=True)
    assert proto.feed(build_frame(PTYPE_PUBLISHED_LINE, first)) == []

    second = _published_line_payload(1, b"Shop PC", b"world", more=False)
    msgs = proto.feed(build_frame(PTYPE_PUBLISHED_LINE, second))

    assert len(msgs) == 1
    assert msgs[0].kind == MessageKind.PUBLISHED_LINE
    assert msgs[0].source_id == 1
    assert msgs[0].source_name == "Shop PC"
    assert msgs[0].text == "hello world"


def test_published_line_reset_discards_a_partial_fragment():
    proto = MakeraProtocol()
    first = _published_line_payload(1, b"Shop PC", b"partial", more=True)
    assert proto.feed(build_frame(PTYPE_PUBLISHED_LINE, first)) == []

    proto.reset()

    second = _published_line_payload(1, b"Shop PC", b"fresh", more=False)
    msgs = proto.feed(build_frame(PTYPE_PUBLISHED_LINE, second))
    assert len(msgs) == 1
    assert msgs[0].text == "fresh"  # not "partialfresh": the old fragment was dropped


def test_event_dispatched_and_not_swallowed_by_the_unknown_type_fallback():
    msgs = MakeraProtocol().feed(build_frame(PTYPE_EVENT, b"\x01\x02"))
    assert len(msgs) == 1
    assert msgs[0].kind == MessageKind.EVENT
    assert msgs[0].payload == b"\x01\x02"
