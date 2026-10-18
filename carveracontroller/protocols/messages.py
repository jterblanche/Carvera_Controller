"""Parsed messages produced by communication protocol RX parsers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class MessageKind(Enum):
    LINE = auto()
    LOAD_CHUNK = auto()
    LOAD_EOF = auto()
    LOAD_ERROR = auto()
    HELLO_ACK = auto()
    CLIENT_LIST = auto()
    # A complete published console line (0x69), reassembled from however
    # many fragments it took — see protocols/makera.py's fragment buffer.
    # source_id/source_name carry who sent the original command; text is
    # the fully-joined line.
    PUBLISHED_LINE = auto()
    # A published system event (0x68: upload finished, play started, job
    # ended, alarm/halt). Only upload-finished, play-started and
    # control-changed are decoded by this controller so far — see
    # protocols/handshake.py — but every kind is intercepted here so none
    # can fall through to the unknown-type-becomes-console-LINE path.
    EVENT = auto()
    # An identified client's relay (0x67), repeated to every *other*
    # identified client with an 8-byte source id prefixed — see
    # protocols/relay.py for what this controller puts inside one.
    # source_id carries who sent it; payload is the opaque relay bytes.
    RELAY = auto()
    # The machine's presence check (0x6D); payload is its number and
    # whatever follows it. Answered by the reading thread itself.
    PRESENCE_CHECK = auto()
    # The machine started serving a download's digest (0xB1) on the
    # ordinary link, where normally no framed transfer is reading the
    # wire -- one that owns the wire reads it directly while streamIO is
    # paused, never through this parser (see protocols/makera.py's
    # _messages_from_packet). A narrow startup-race exception, where this
    # can still be a transfer's own digest, is handled in Controller's
    # _refuse_unsolicited_download, not here. So this is ordinarily a
    # download nobody is waiting for: already given up on, never
    # requested, or offered again after a cancel. payload is the raw
    # advertised digest, kept only for logging. Answered by the reading
    # thread itself, the same as PRESENCE_CHECK.
    UNSOLICITED_DOWNLOAD = auto()


@dataclass(frozen=True)
class ParsedMessage:
    kind: MessageKind
    text: str = ""
    # Raw payload bytes for messages whose structure is decoded elsewhere
    # (HELLO_ACK, CLIENT_LIST, EVENT) — see protocols/handshake.py.
    payload: bytes = b""
    # PUBLISHED_LINE only: who sent the original command this line is from.
    source_id: int = 0
    source_name: str = ""
