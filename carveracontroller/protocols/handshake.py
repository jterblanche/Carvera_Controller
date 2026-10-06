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

# The features byte, after `link` in the hello and after `mode` in the
# hello ack. In the hello, bit 0 says this controller takes part in the
# job-start wait. In the ack, bit 0 says the machine holds a job's start
# until the controllers that take part are ready. Firmware without the wait
# ignores the byte in the hello and sends a three-byte ack, read as 0.
HELLO_FEATURE_JOB_START_WAIT = 0x01

# Event (`0x68`) `kind` bytes this controller decodes. The machine defines
# two more (3, job ended; 4, alarm/halt) that this controller does not act
# on yet -- see MessageKind.EVENT.
EVENT_KIND_UPLOAD_FINISHED = 1
EVENT_KIND_PLAY_STARTED = 2
EVENT_KIND_CONTROL_CHANGED = 5
EVENT_KIND_CLIENT_JOINED = 6
EVENT_KIND_CLIENT_LEFT = 7
EVENT_KIND_JOB_START = 8

# Job-start event `phase` values.
JOB_START_WAITING = 0
JOB_START_STARTING = 1
JOB_START_CANCELLED = 2

# Job-start event `reason` values: 0 with phase waiting; 1 to 3 with phase
# starting; 4 to 6 with phase cancelled.
JOB_START_REASON_WAITING = 0
JOB_START_REASON_ALL_READY = 1
JOB_START_REASON_TIME_LIMIT = 2
JOB_START_REASON_START_NOW = 3
JOB_START_REASON_ABORT = 4
JOB_START_REASON_STARTER_LEFT = 5
JOB_START_REASON_HALT = 6

# hello / client-list-entry `link` values.
LINK_WIFI = 0
LINK_USB = 1

_MAX_NAME_BYTES = 31


@dataclass(frozen=True)
class HelloAck:
    protocol_version: int
    result: int
    mode: int
    features: int = 0


def decode_hello_ack(payload: bytes) -> HelloAck | None:
    """Decode a hello-ack payload: protocol_version(1) + result(1) + mode(1)
    + features(1, optional). Returns None if it is too short to be valid. A
    three-byte ack, from firmware without the features byte, has features 0."""
    if len(payload) < 3:
        return None
    features = payload[3] if len(payload) > 3 else 0
    return HelloAck(protocol_version=payload[0], result=payload[1], mode=payload[2], features=features)


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
    to the card just completed. ``path`` triggers a passive controller's
    own fetch of the same file; ``checksum`` (the raw digest, empty if the
    firmware announced ``checksum_type`` 0, i.e. none) lets that controller
    skip the fetch instead when its own local copy already matches (see
    main.py's ``on_passive_file_published``). ``size`` is decoded but still
    unused."""

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
    started playing ``path``, from whichever client commanded it. ``size``
    and ``checksum`` (the raw MD5, empty when the machine has none) let a
    controller draw a local copy with the same content at once. Firmware
    that sends only the path gives ``size`` None and an empty checksum."""

    path: str
    size: int | None = None
    checksum: bytes = b""


def decode_play_started_event(payload: bytes) -> PlayStarted | None:
    """Decode one event (`0x68`) payload as play-started: kind(1) +
    path_len(1) + path, then, from firmware that sends them, size(4, BE) +
    checksum_type(1: 0=none, 1=md5) + checksum(0 or 16 B), the same layout
    as upload-finished. Returns None if the first byte is not
    ``EVENT_KIND_PLAY_STARTED``, or the payload is too short for its own
    path or for a checksum it declares -- dropped silently, same as every
    other decoder here."""
    if len(payload) < 1 + 1:
        return None
    if payload[0] != EVENT_KIND_PLAY_STARTED:
        return None
    decoded = _decode_file_part(payload, size_optional=True)
    if decoded is None:
        return None
    path, size, checksum, _ = decoded
    return PlayStarted(path=path, size=size, checksum=checksum)


@dataclass(frozen=True)
class JobStartEvent:
    """One `0x68` event, kind `EVENT_KIND_JOB_START`: the machine is holding
    the start of a job until the controllers that take part are ready
    (``phase`` waiting), or the hold has just ended (starting or cancelled,
    with the ``reason``). ``path``, ``size`` and ``checksum`` name the file,
    as play-started will. ``start_id`` (never 0) is what a ready frame
    answers; ``seconds_left`` is the time to the machine's limit while
    waiting; ``starter_id`` is the controller that started the job, and
    ``not_ready_ids`` the controllers the machine is still waiting for."""

    path: str
    size: int
    checksum: bytes
    start_id: int
    phase: int
    reason: int
    seconds_left: int
    starter_id: int
    not_ready_ids: tuple[int, ...]


def decode_job_start_event(payload: bytes) -> JobStartEvent | None:
    """Decode one event (`0x68`) payload as a job-start event: the
    upload-finished layout after the kind byte (path_len, path, size,
    checksum_type, checksum), then start_id(2, BE) + phase(1) + reason(1) +
    seconds_left(1) + starter_id(8, BE) + not_ready_count(1) +
    not_ready_ids(8 each, BE). Returns None for another kind, a payload cut
    short, or a start_id of 0."""
    if len(payload) < 1 + 1 or payload[0] != EVENT_KIND_JOB_START:
        return None
    decoded = _decode_file_part(payload, size_optional=False)
    if decoded is None:
        return None
    path, size, checksum, offset = decoded
    if size is None or offset + 14 > len(payload):
        return None
    start_id = int.from_bytes(payload[offset : offset + 2], "big")
    phase = payload[offset + 2]
    reason = payload[offset + 3]
    seconds_left = payload[offset + 4]
    starter_id = int.from_bytes(payload[offset + 5 : offset + 13], "big")
    count = payload[offset + 13]
    offset += 14
    if start_id == 0 or offset + 8 * count > len(payload):
        return None
    not_ready = tuple(int.from_bytes(payload[offset + 8 * i : offset + 8 * i + 8], "big") for i in range(count))
    return JobStartEvent(
        path=path,
        size=size,
        checksum=checksum,
        start_id=start_id,
        phase=phase,
        reason=reason,
        seconds_left=seconds_left,
        starter_id=starter_id,
        not_ready_ids=not_ready,
    )


def _decode_file_part(payload: bytes, size_optional: bool) -> tuple[str, int | None, bytes, int] | None:
    """The path_len(1) + path + size(4, BE) + checksum_type(1) + checksum(0
    or 16 B) that follows the kind byte of the upload-finished, play-started
    and job-start events. Returns (path, size, checksum, offset just past
    it), or None if the payload is too short. With ``size_optional``, a
    payload too short to hold the size and checksum type after the path
    gives size None and no checksum (play-started from firmware that sends
    only the path)."""
    path_len = payload[1]
    offset = 2
    if offset + path_len > len(payload):
        return None
    path = payload[offset : offset + path_len].decode("utf-8", errors="replace")
    offset += path_len
    if offset + 4 + 1 > len(payload):
        if size_optional:
            return path, None, b"", offset
        return None
    size = int.from_bytes(payload[offset : offset + 4], "big")
    offset += 4
    checksum_type = payload[offset]
    offset += 1
    checksum_len = 16 if checksum_type == 1 else 0
    if offset + checksum_len > len(payload):
        return None
    checksum = payload[offset : offset + checksum_len]
    return path, size, checksum, offset + checksum_len


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
