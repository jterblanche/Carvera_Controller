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

# hello ack `mode` values: whether the machine hands control to whoever last
# acted (single-user) or keeps it with the holder until they release it or
# disconnect (multi-user). Every machine reports single-user until it is
# configured otherwise.
HELLO_MODE_SINGLE_USER = 0
HELLO_MODE_MULTI_USER = 1

# Event (`0x68`) `kind` bytes this controller decodes. The machine defines
# two more (3, job ended; 4, alarm/halt) that this controller does not act
# on yet -- see MessageKind.EVENT.
EVENT_KIND_UPLOAD_FINISHED = 1
EVENT_KIND_PLAY_STARTED = 2
EVENT_KIND_CONTROL_CHANGED = 5
EVENT_KIND_CLIENT_JOINED = 6
EVENT_KIND_CLIENT_LEFT = 7

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
    published by the machine to every identified client.
    ``source_id``/``source_name`` are on every fragment, not just the
    first, so a fragment never needs to be paired with an earlier
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


@dataclass(frozen=True)
class UploadFinished:
    """One `0x68` event, kind `EVENT_KIND_UPLOAD_FINISHED`: a file transfer
    to the card just completed. ``size``/``checksum`` are decoded but not
    used by this controller yet -- only ``path`` is, to trigger a passive
    controller's own fetch of the same file."""

    path: str
    size: int
    checksum: bytes


def decode_upload_finished_event(payload: bytes) -> UploadFinished | None:
    """Decode one event (`0x68`) payload as upload-finished: kind(1) +
    path_len(1) + path + size(4, BE) + checksum_type(1: 0=none, 1=md5) +
    checksum(0 or 16 B). Returns None if the first byte is not
    ``EVENT_KIND_UPLOAD_FINISHED``, or the payload is too short for its own
    fields -- the caller drops it silently, same as every other decoder
    here."""
    if len(payload) < 1 + 1:
        return None
    if payload[0] != EVENT_KIND_UPLOAD_FINISHED:
        return None
    path_len = payload[1]
    offset = 2
    if offset + path_len + 4 + 1 > len(payload):
        return None
    path = payload[offset : offset + path_len].decode("utf-8", errors="replace")
    offset += path_len
    size = int.from_bytes(payload[offset : offset + 4], "big")
    offset += 4
    checksum_type = payload[offset]
    offset += 1
    checksum_len = 16 if checksum_type == 1 else 0
    if offset + checksum_len > len(payload):
        return None
    checksum = payload[offset : offset + checksum_len]
    return UploadFinished(path=path, size=size, checksum=checksum)


@dataclass(frozen=True)
class PlayStarted:
    """One `0x68` event, kind `EVENT_KIND_PLAY_STARTED`: the machine just
    started playing ``path``, from whichever client commanded it."""

    path: str


def decode_play_started_event(payload: bytes) -> PlayStarted | None:
    """Decode one event (`0x68`) payload as play-started: kind(1) +
    path_len(1) + path. Returns None if the first byte is not
    ``EVENT_KIND_PLAY_STARTED``, or the payload is too short for its own
    path -- dropped silently, same as every other decoder here."""
    if len(payload) < 1 + 1:
        return None
    if payload[0] != EVENT_KIND_PLAY_STARTED:
        return None
    path_len = payload[1]
    offset = 2
    if offset + path_len > len(payload):
        return None
    path = payload[offset : offset + path_len].decode("utf-8", errors="replace")
    return PlayStarted(path=path)


@dataclass(frozen=True)
class ControlChanged:
    """One `0x68` event, kind `EVENT_KIND_CONTROL_CHANGED`: who holds control
    now. ``holder_id == 0`` (with an empty ``holder_name``) means nobody
    does -- the same "nobody" encoding the machine's own
    ``build_control_changed_event`` uses, not a value this controller
    invents.
    """

    holder_id: int
    holder_name: str


def decode_control_changed_event(payload: bytes) -> ControlChanged | None:
    """Decode one event (`0x68`) payload as a control-changed event:
    kind(1) + holder_id(8, big-endian) + holder_name_len(1) + holder_name.

    Returns None if the payload is too short, its first byte is not
    ``EVENT_KIND_CONTROL_CHANGED``, or the declared name is longer than fits
    -- the caller then drops it silently, the same tolerant style every
    other decoder in this module uses for a malformed message.
    """
    if not payload or payload[0] != EVENT_KIND_CONTROL_CHANGED:
        return None
    decoded = _decode_id_and_name(payload)
    if decoded is None:
        return None
    holder_id, holder_name = decoded
    return ControlChanged(holder_id=holder_id, holder_name=holder_name)


@dataclass(frozen=True)
class ClientPresenceChanged:
    """One `0x68` event, kind `EVENT_KIND_CLIENT_JOINED` or
    `EVENT_KIND_CLIENT_LEFT`: an identified controller has just joined the
    machine, or left it. The machine publishes these to every identified
    client, the one that joined included, and never for a controller that
    has not identified itself, nor for one reconnecting under an id it
    already holds.
    """

    client_id: int
    name: str
    joined: bool


def decode_client_presence_event(payload: bytes) -> ClientPresenceChanged | None:
    """Decode one event (`0x68`) payload as a client-joined or client-left
    event: kind(1) + client_id(8, big-endian) + name_len(1) + name, the
    same layout as a control-changed event.

    Returns None if the payload is too short, its first byte is neither
    kind, or the declared name is longer than fits.
    """
    if not payload or payload[0] not in (EVENT_KIND_CLIENT_JOINED, EVENT_KIND_CLIENT_LEFT):
        return None
    decoded = _decode_id_and_name(payload)
    if decoded is None:
        return None
    client_id, name = decoded
    return ClientPresenceChanged(client_id=client_id, name=name, joined=payload[0] == EVENT_KIND_CLIENT_JOINED)


def _decode_id_and_name(payload: bytes) -> tuple[int, str] | None:
    """The id(8, big-endian) + name_len(1) + name that follows the kind byte
    in every event naming a controller. None if the payload is too short or
    the declared name is longer than fits."""
    if len(payload) < 1 + 8 + 1:
        return None
    client_id = int.from_bytes(payload[1:9], "big")
    offset = 9
    name_len = payload[offset]
    offset += 1
    if name_len > _MAX_NAME_BYTES or offset + name_len > len(payload):
        return None
    return client_id, payload[offset : offset + name_len].decode("utf-8", errors="replace")
