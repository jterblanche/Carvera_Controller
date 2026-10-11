from carveracontroller.protocols.handshake import (
    EVENT_KIND_CLIENT_JOINED,
    EVENT_KIND_CLIENT_LEFT,
    EVENT_KIND_CONTROL_CHANGED,
    ClientEntry,
    ClientPresenceChanged,
    ControlChanged,
    PublishedLineFragment,
    decode_client_list,
    decode_client_presence_event,
    decode_control_changed_event,
    decode_hello_ack,
    decode_published_line,
)


def test_decode_hello_ack():
    ack = decode_hello_ack(bytes([1, 0, 1]))
    assert ack is not None
    assert ack.protocol_version == 1
    assert ack.result == 0
    assert ack.mode == 1


def test_decode_hello_ack_too_short_returns_none():
    assert decode_hello_ack(b"") is None
    assert decode_hello_ack(bytes([1, 0])) is None


def test_decode_client_list_empty():
    assert decode_client_list(b"") == ()
    assert decode_client_list(bytes([0])) == ()


def test_decode_client_list_two_entries():
    entry1 = (0x0102030405060708).to_bytes(8, "big") + bytes([len(b"Office PC")]) + b"Office PC" + bytes([0, 1])
    entry2 = (0x0A0B0C0D0E0F1011).to_bytes(8, "big") + bytes([len(b"Shop")]) + b"Shop" + bytes([1, 0])
    payload = bytes([2]) + entry1 + entry2

    entries = decode_client_list(payload)

    assert entries == (
        ClientEntry(id=0x0102030405060708, name="Office PC", link=0, has_control=True),
        ClientEntry(id=0x0A0B0C0D0E0F1011, name="Shop", link=1, has_control=False),
    )


def test_decode_client_list_stops_on_truncated_entry():
    # count says 2 entries but only one full entry is present.
    entry1 = (1).to_bytes(8, "big") + bytes([1]) + b"A" + bytes([0, 0])
    payload = bytes([2]) + entry1 + bytes([1, 2, 3])

    entries = decode_client_list(payload)

    assert len(entries) == 1
    assert entries[0].name == "A"


def _client_list_entry(client_id, name, link, has_control):
    raw = name.encode()
    return client_id.to_bytes(8, "big") + bytes([len(raw)]) + raw + bytes([link, 1 if has_control else 0])


def test_decode_client_list_reads_the_addresses_after_the_last_entry():
    payload = (
        bytes([3])
        + _client_list_entry(1, "PC", 0, False)
        + _client_list_entry(2, "PC", 0, True)
        + _client_list_entry(3, "PC", 1, False)
        + bytes([192, 168, 1, 23])
        + bytes([192, 168, 1, 57])
        + bytes([0, 0, 0, 0])
    )

    entries = decode_client_list(payload)

    assert [(entry.id, entry.ip) for entry in entries] == [(1, "192.168.1.23"), (2, "192.168.1.57"), (3, "")]
    assert entries[1].has_control is True


def test_decode_client_list_without_addresses_has_no_ips():
    # Firmware from before the addresses were added ends at the last entry.
    payload = bytes([2]) + _client_list_entry(1, "PC", 0, False) + _client_list_entry(2, "PC", 1, False)

    entries = decode_client_list(payload)

    assert [(entry.id, entry.link, entry.ip) for entry in entries] == [(1, 0, ""), (2, 1, "")]


def test_decode_client_list_ignores_addresses_cut_short():
    payload = bytes([2]) + _client_list_entry(1, "PC", 0, False) + _client_list_entry(2, "PC", 0, False)
    payload += bytes([192, 168, 1, 23, 192, 168])

    entries = decode_client_list(payload)

    assert [entry.ip for entry in entries] == ["", ""]


def test_decode_client_list_ignores_bytes_after_the_addresses():
    payload = bytes([1]) + _client_list_entry(1, "PC", 0, False) + bytes([10, 0, 0, 5]) + b"later"

    entries = decode_client_list(payload)

    assert entries == (ClientEntry(id=1, name="PC", link=0, has_control=False, ip="10.0.0.5"),)


def test_decode_client_list_reads_no_addresses_after_a_truncated_entry():
    payload = bytes([2]) + _client_list_entry(1, "PC", 0, False) + bytes([10, 0, 0, 5, 10, 0, 0, 6])

    entries = decode_client_list(payload)

    assert [entry.ip for entry in entries] == [""]


def test_decode_published_line():
    payload = (
        (0x0102030405060708).to_bytes(8, "big") + bytes([len(b"Office PC")]) + b"Office PC" + bytes([0]) + b"version"
    )

    fragment = decode_published_line(payload)

    assert fragment == PublishedLineFragment(
        source_id=0x0102030405060708, source_name="Office PC", more=False, text=b"version"
    )


def test_decode_published_line_more_flag_set():
    payload = (1).to_bytes(8, "big") + bytes([1]) + b"A" + bytes([1]) + b"partial"

    fragment = decode_published_line(payload)

    assert fragment is not None
    assert fragment.more is True
    assert fragment.text == b"partial"


def test_decode_published_line_empty_text_is_valid():
    payload = (1).to_bytes(8, "big") + bytes([1]) + b"A" + bytes([0])

    fragment = decode_published_line(payload)

    assert fragment is not None
    assert fragment.text == b""


def test_decode_published_line_too_short_returns_none():
    assert decode_published_line(b"") is None
    # 8 bytes of id plus a name_len byte, but nothing for the mandatory
    # `more` flag that must follow the (empty) name.
    assert decode_published_line((1).to_bytes(8, "big") + bytes([0])) is None


def test_decode_published_line_oversized_name_returns_none():
    payload = (1).to_bytes(8, "big") + bytes([32]) + b"x" * 32 + bytes([0])
    assert decode_published_line(payload) is None


def test_decode_published_line_truncated_name_returns_none():
    # name_len says 5 but only 2 bytes of name (and nothing else) follow.
    payload = (1).to_bytes(8, "big") + bytes([5]) + b"ab"
    assert decode_published_line(payload) is None


def test_decode_control_changed_event():
    payload = bytes([EVENT_KIND_CONTROL_CHANGED]) + (0x0102030405060708).to_bytes(8, "big") + bytes([9]) + b"Office PC"

    event = decode_control_changed_event(payload)

    assert event == ControlChanged(holder_id=0x0102030405060708, holder_name="Office PC")


def test_decode_control_changed_event_nobody_has_control():
    # The machine's own encoding for "nobody": holder_id 0, empty name.
    payload = bytes([EVENT_KIND_CONTROL_CHANGED]) + (0).to_bytes(8, "big") + bytes([0])

    event = decode_control_changed_event(payload)

    assert event == ControlChanged(holder_id=0, holder_name="")


def test_decode_control_changed_event_wrong_kind_returns_none():
    # kind 4 (alarm/halt) is a real event kind, just not this one.
    payload = bytes([4]) + (1).to_bytes(8, "big") + bytes([0])
    assert decode_control_changed_event(payload) is None


def test_decode_control_changed_event_too_short_returns_none():
    assert decode_control_changed_event(b"") is None
    # kind + holder_id, but nothing for the mandatory name-length byte.
    assert decode_control_changed_event(bytes([EVENT_KIND_CONTROL_CHANGED]) + (1).to_bytes(8, "big")) is None


def test_decode_control_changed_event_oversized_name_returns_none():
    payload = bytes([EVENT_KIND_CONTROL_CHANGED]) + (1).to_bytes(8, "big") + bytes([32]) + b"x" * 32
    assert decode_control_changed_event(payload) is None


def test_decode_control_changed_event_truncated_name_returns_none():
    # name_len says 5 but only 2 bytes of name follow.
    payload = bytes([EVENT_KIND_CONTROL_CHANGED]) + (1).to_bytes(8, "big") + bytes([5]) + b"ab"
    assert decode_control_changed_event(payload) is None


def test_decode_client_joined_event():
    payload = bytes([EVENT_KIND_CLIENT_JOINED]) + (0x0102030405060708).to_bytes(8, "big") + bytes([9]) + b"Office PC"

    event = decode_client_presence_event(payload)

    assert event == ClientPresenceChanged(client_id=0x0102030405060708, name="Office PC", joined=True)


def test_decode_client_left_event():
    payload = bytes([EVENT_KIND_CLIENT_LEFT]) + (0x0102030405060708).to_bytes(8, "big") + bytes([8]) + b"Workshop"

    event = decode_client_presence_event(payload)

    assert event == ClientPresenceChanged(client_id=0x0102030405060708, name="Workshop", joined=False)


def test_decode_client_presence_event_wrong_kind_returns_none():
    # Same layout, but a control-changed event is not a presence event.
    payload = bytes([EVENT_KIND_CONTROL_CHANGED]) + (1).to_bytes(8, "big") + bytes([0])
    assert decode_client_presence_event(payload) is None


def test_decode_control_changed_event_ignores_presence_kinds():
    # And the other way round: the shared layout must not let a joined or
    # left event pass for a control change.
    for kind in (EVENT_KIND_CLIENT_JOINED, EVENT_KIND_CLIENT_LEFT):
        payload = bytes([kind]) + (1).to_bytes(8, "big") + bytes([0])
        assert decode_control_changed_event(payload) is None


def test_decode_client_presence_event_too_short_returns_none():
    assert decode_client_presence_event(b"") is None
    assert decode_client_presence_event(bytes([EVENT_KIND_CLIENT_JOINED]) + (1).to_bytes(8, "big")) is None


def test_decode_client_presence_event_oversized_name_returns_none():
    payload = bytes([EVENT_KIND_CLIENT_LEFT]) + (1).to_bytes(8, "big") + bytes([32]) + b"x" * 32
    assert decode_client_presence_event(payload) is None
