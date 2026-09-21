"""Decoders for the identify-handshake, client-list and published-console-
line wire messages: hello ack (`0x61`), client-list reply (`0x64`) and one
published-console-line fragment (`0x69`). This module only decodes;
encoding lives in ``protocols/makera.py`` alongside the rest of the Makera
frame builders.
"""

from __future__ import annotations

from dataclasses import dataclass

# hello ack `result` values.
HELLO_ACCEPTED = 0
HELLO_REJECTED_CAP = 1
HELLO_REJECTED_OLD_CONTROLLER = 2

# hello / client-list-entry `link` values.
LINK_WIFI = 0
LINK_USB = 1

_MAX_NAME_BYTES = 31


@dataclass(frozen=True)
class HelloAck:
    protocol_version: int
    result: int
    mode: int


def decode_hello_ack(payload: bytes) -> HelloAck | None:
    """Decode a hello-ack payload. Returns None if it is too short to be valid."""
    if len(payload) < 3:
        return None
    return HelloAck(protocol_version=payload[0], result=payload[1], mode=payload[2])


@dataclass(frozen=True)
class ClientEntry:
    id: int
    name: str
    link: int
    has_control: bool


def decode_client_list(payload: bytes) -> tuple[ClientEntry, ...]:
    """Decode a client-list-reply payload: count(1) + entries.

    Each entry is id(8) + name_len(1) + name(<=31) + link(1) + has_control(1).
    Malformed trailing data (short read) stops decoding and returns whatever
    complete entries were parsed, rather than raising.
    """
    if not payload:
        return ()
    count = payload[0]
    entries: list[ClientEntry] = []
    offset = 1
    for _ in range(count):
        if offset + 9 > len(payload):
            break
        client_id = int.from_bytes(payload[offset : offset + 8], "big")
        offset += 8
        name_len = payload[offset]
        offset += 1
        if name_len > _MAX_NAME_BYTES or offset + name_len + 2 > len(payload):
            break
        name = payload[offset : offset + name_len].decode("utf-8", errors="replace")
        offset += name_len
        link = payload[offset]
        offset += 1
        has_control = payload[offset] != 0
        offset += 1
        entries.append(ClientEntry(id=client_id, name=name, link=link, has_control=has_control))
    return tuple(entries)


@dataclass(frozen=True)
class PublishedLineFragment:
    """One `0x69` frame: a slice of a command's text or its reply, as
    published by the machine to every identified client (protocol contract
    section 6.10). ``source_id``/``source_name`` are on every fragment, not
    just the first, so a fragment never needs to be paired with an earlier
    one to know who it's from.
    """

    source_id: int
    source_name: str
    more: bool
    text: bytes


def decode_published_line(payload: bytes) -> PublishedLineFragment | None:
    """Decode one published-console-line payload: source_id(8) +
    source_name_len(1) + source_name(<=31) + more(1) + text(remainder).

    Returns None if the payload is too short to hold its own fixed fields,
    or the declared name is longer than fits — the caller then drops the
    fragment silently, the same tolerant style ``decode_client_list`` above
    already uses for a malformed entry.
    """
    if len(payload) < 8 + 1 + 1:
        return None
    source_id = int.from_bytes(payload[0:8], "big")
    offset = 8
    name_len = payload[offset]
    offset += 1
    if name_len > _MAX_NAME_BYTES or offset + name_len + 1 > len(payload):
        return None
    source_name = payload[offset : offset + name_len].decode("utf-8", errors="replace")
    offset += name_len
    more = payload[offset] != 0
    offset += 1
    text = payload[offset:]
    return PublishedLineFragment(source_id=source_id, source_name=source_name, more=more, text=text)
