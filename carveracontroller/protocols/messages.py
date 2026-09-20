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


@dataclass(frozen=True)
class ParsedMessage:
    kind: MessageKind
    text: str = ""
    # Raw payload bytes for messages whose structure is decoded elsewhere
    # (HELLO_ACK, CLIENT_LIST) — see protocols/handshake.py.
    payload: bytes = b""
