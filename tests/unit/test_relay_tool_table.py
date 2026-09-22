"""Pure encode/decode tests for the relayed tool-table summary
(protocols/relay.py) -- the convention this controller puts inside a relay
(`0x67`) frame's opaque payload. No socket, no Controller: these are plain
functions over bytes."""

from __future__ import annotations

from carveracontroller.protocols.relay import (
    RELAY_KIND_TOOL_TABLE,
    decode_tool_table_relay,
    encode_tool_table_relay,
    max_entry_text_bytes,
)


def test_round_trip_empty_table():
    payload = encode_tool_table_relay({})
    assert payload == bytes([RELAY_KIND_TOOL_TABLE, 0])
    assert decode_tool_table_relay(payload) == {}


def test_round_trip_several_entries():
    entries = {1: "T1 · Flat End Mill", 2: "T2 · Ball End Mill — 3mm", 9999: "Probe"}
    payload = encode_tool_table_relay(entries)
    assert decode_tool_table_relay(payload) == entries


def test_decode_rejects_a_different_relay_kind():
    # Some other relay traffic this controller does not understand yet --
    # dropped, not raised.
    assert decode_tool_table_relay(bytes([0, 0])) is None


def test_decode_rejects_too_short_a_payload():
    assert decode_tool_table_relay(bytes([RELAY_KIND_TOOL_TABLE])) is None


def test_long_text_is_truncated_not_rejected():
    text = "x" * 200
    payload = encode_tool_table_relay({1: text})
    decoded = decode_tool_table_relay(payload)
    assert decoded is not None
    assert len(decoded[1].encode("utf-8")) <= max_entry_text_bytes
    assert decoded[1] == text[:max_entry_text_bytes]


def test_truncation_does_not_split_a_multibyte_character():
    # A description ending exactly on a multi-byte boundary must not be cut
    # mid-character, which would otherwise leave invalid UTF-8 on the wire.
    text = "a" * (max_entry_text_bytes - 1) + "éé"  # each é is 2 bytes in UTF-8
    payload = encode_tool_table_relay({1: text})
    decoded = decode_tool_table_relay(payload)
    assert decoded is not None
    # Must decode cleanly and never contain a partial character.
    decoded[1].encode("utf-8")  # raises if invalid; assertion is that this doesn't raise


def test_entries_beyond_the_payload_cap_are_dropped_not_raised():
    # Far more entries than fit in the 527 B relay payload cap.
    entries = dict.fromkeys(range(50), "y" * max_entry_text_bytes)
    payload = encode_tool_table_relay(entries)
    assert len(payload) <= 527
    decoded = decode_tool_table_relay(payload)
    assert decoded is not None
    assert len(decoded) < len(entries)
    # Every entry that did make it round-trips exactly.
    for number, text in decoded.items():
        assert entries[number] == text


def test_decode_stops_at_a_truncated_trailing_entry():
    # count says 2 entries but only one complete entry is actually present
    # -- a malformed/truncated relay payload, not a real one this
    # controller would ever build.
    payload = bytearray([RELAY_KIND_TOOL_TABLE, 2])
    payload += (1).to_bytes(2, "big") + bytes([1]) + b"a"
    payload += (2).to_bytes(2, "big") + bytes([5]) + b"bc"  # declares 5 bytes, only 2 present
    decoded = decode_tool_table_relay(bytes(payload))
    assert decoded == {1: "a"}
