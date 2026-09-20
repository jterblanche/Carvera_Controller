from carveracontroller.protocols.handshake import (
    ClientEntry,
    decode_client_list,
    decode_hello_ack,
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
