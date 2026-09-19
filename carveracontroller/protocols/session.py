"""Connection-scoped protocol session with autodetection and mid-session switching."""

from __future__ import annotations

import logging
from typing import Callable

from .base import CommunicationProtocol
from .detector import detect_protocol_name
from .framing import FRAME_HEADER
from .messages import ParsedMessage
from .registry import DEFAULT_PROTOCOL, create_protocol

logger = logging.getLogger(__name__)

OnChangeCallback = Callable[[str, bool], None]

_FRAME_HEADER_BYTES = FRAME_HEADER.to_bytes(2, "big")


def _normalize_message(message: ParsedMessage) -> ParsedMessage:
    """Deliver text payloads without trailing CR/LF from the wire/frame layer."""
    if not message.text:
        return message
    cleaned = message.text.rstrip("\r\n")
    if cleaned == message.text:
        return message
    return ParsedMessage(message.kind, cleaned)


def protocol_name_from_announcement(text: str) -> str | None:
    """
    Parse firmware protocol announcements (M485 / M485.1 / M485.2).

    Returns a registered protocol name, or None if the text is unrelated.
    """
    if not text:
        return None
    lower = text.lower()
    if "makera communication protocol" in lower or "current communication protocol: makera" in lower:
        return "makera"
    if "smoothie communication protocol" in lower or "current communication protocol: smoothie" in lower:
        return "smoothie"
    return None


class ProtocolSession:
    """
    Owns the active wire protocol for one machine connection.

    Responsibilities:
      - encode commands / realtime / file-start for the selected protocol
      - parse inbound bytes via ``feed``
      - initial selection (caller uses ``detect_and_select`` after transport open)
      - mid-session switches from M485 announcements or unexpected Makera frames
    """

    def __init__(
        self,
        name: str = DEFAULT_PROTOCOL,
        on_change: OnChangeCallback | None = None,
    ) -> None:
        self._on_change = on_change
        self._protocol: CommunicationProtocol = create_protocol(name)
        self.ready: bool = False

    @property
    def name(self) -> str:
        return self._protocol.name

    @property
    def uses_framed_transfer(self) -> bool:
        return self._protocol.uses_framed_transfer

    @property
    def frame_confirmed(self) -> bool:
        return self._protocol.frame_confirmed

    @property
    def protocol(self) -> CommunicationProtocol:
        return self._protocol

    def select(self, name: str) -> bool:
        """
        Select a protocol by name.

        Returns True if the active protocol changed (or became ready for the
        first time with this name).
        """
        if self._protocol.name == name and self.ready:
            return False
        self._protocol = create_protocol(name)
        self._protocol.ready = True
        self.ready = True
        logger.info("Using %s communication protocol", name)
        if self._on_change is not None:
            self._on_change(name, self._protocol.uses_framed_transfer)
        return True

    def reset(self) -> None:
        """Reset to the default protocol for a disconnected state."""
        if self._protocol is not None:
            self._protocol.reset()
        self._protocol = create_protocol(DEFAULT_PROTOCOL)
        self.ready = False
        if self._on_change is not None:
            self._on_change(self._protocol.name, False)

    def reset_parser(self) -> None:
        """Clear RX parser state without changing the selected protocol."""
        was_ready = self.ready
        self._protocol.reset()
        self._protocol.ready = was_ready
        self.ready = was_ready

    def detect_and_select(self, stream) -> str:
        """Probe the transport and select the matching protocol."""
        try:
            name = detect_protocol_name(stream)
        except Exception:
            logger.exception("Protocol detection failed; defaulting to %s", DEFAULT_PROTOCOL)
            name = DEFAULT_PROTOCOL
        self.select(name)
        return name

    def encode_command(self, data: bytes) -> bytes:
        return self._protocol.encode_command(data)

    def encode_realtime(self, char: int) -> bytes:
        return self._protocol.encode_realtime(char)

    def encode_file_command(self, data: bytes) -> bytes:
        return self._protocol.encode_file_command(data)

    def feed(self, data: bytes, allow_wire_switch: bool = True) -> list[ParsedMessage]:
        """
        Parse inbound bytes, switching protocol mid-session when needed.

        ``allow_wire_switch`` should be False during file transfers so XMODEM
        binary cannot spuriously trigger a Makera header switch.
        """
        if allow_wire_switch and not self._protocol.uses_framed_transfer and _FRAME_HEADER_BYTES in data:
            # Machine is speaking Makera while we were still on Smoothie.
            self.select("makera")

        messages = [_normalize_message(m) for m in self._protocol.feed(data)]
        if allow_wire_switch:
            for message in messages:
                announced = protocol_name_from_announcement(message.text or "")
                if announced and announced != self.name:
                    self.select(announced)
        return messages
