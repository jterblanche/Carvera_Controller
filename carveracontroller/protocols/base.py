"""Abstract communication protocol interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .messages import ParsedMessage


class CommunicationProtocol(ABC):
    """Strategy for encoding commands and parsing machine responses."""

    name: str = "unknown"
    uses_framed_transfer: bool = False

    def __init__(self) -> None:
        self.ready: bool = False
        # True once at least one complete, CRC-valid frame has been received
        # from the machine. Only ever set by a framed protocol (Makera);
        # stays False for Smoothie, which has no frames. Proves the link is
        # actually framed, as opposed to the protocol detector's own guess —
        # see docs/protocol/connection-follows-me.md §3.
        self.frame_confirmed: bool = False

    @abstractmethod
    def encode_command(self, data: bytes) -> bytes:
        """Encode a multi-byte / G-code / shell command for the wire."""

    @abstractmethod
    def encode_realtime(self, char: int) -> bytes:
        """Encode a single-byte realtime control (e.g. '?', '!', '~', Ctrl-X)."""

    @abstractmethod
    def encode_file_command(self, data: bytes) -> bytes:
        """Encode an upload/download initiation command."""

    @abstractmethod
    def feed(self, data: bytes) -> list[ParsedMessage]:
        """Consume inbound bytes and return zero or more parsed messages."""

    @abstractmethod
    def reset(self) -> None:
        """Reset RX parser state for a new connection or after errors."""
