"""Makera framed binary communication protocol."""

from __future__ import annotations

from enum import Enum, auto

from .base import CommunicationProtocol
from .framing import (
    FRAME_END,
    FRAME_HEADER,
    MAX_FRAME_DATA_LENGTH,
    PTYPE_AUTO_COMMAND,
    PTYPE_CLIENT_LIST_REPLY,
    PTYPE_CLIENT_LIST_REQ,
    PTYPE_CTRL_MULTI,
    PTYPE_CTRL_RELEASE,
    PTYPE_CTRL_SINGLE,
    PTYPE_EVENT,
    PTYPE_FILE_CAN,
    PTYPE_FILE_DATA,
    PTYPE_FILE_END,
    PTYPE_FILE_MD5,
    PTYPE_FILE_RETRY,
    PTYPE_FILE_START,
    PTYPE_FILE_VIEW,
    PTYPE_HEARTBEAT,
    PTYPE_HELLO,
    PTYPE_HELLO_ACK,
    PTYPE_LOAD_ERROR,
    PTYPE_LOAD_FINISH,
    PTYPE_LOAD_INFO,
    PTYPE_NORMAL_INFO,
    PTYPE_PUBLISHED_LINE,
    build_frame,
    validate_packet_data,
)
from .handshake import decode_published_line
from .messages import MessageKind, ParsedMessage

# Identify-handshake protocol version, carried in the hello frame and
# echoed back in the hello ack, so either side can tell a future breaking
# change in this handshake apart from today's version.
HELLO_PROTOCOL_VERSION = 1


def encode_hello(controller_id: int, name: bytes, link: int) -> bytes:
    """Build a hello (0x60) frame: protocol_version(1) + id(8) + name_len(1) + name + link(1)."""
    if len(name) > 31:
        raise ValueError("hello name must be <= 31 bytes")
    payload = (
        bytes([HELLO_PROTOCOL_VERSION])
        + controller_id.to_bytes(8, "big")
        + bytes([len(name)])
        + name
        + bytes([link & 0xFF])
    )
    return build_frame(PTYPE_HELLO, payload)


def encode_client_list_request() -> bytes:
    """Build a client-list request (0x63) frame. Empty payload."""
    return build_frame(PTYPE_CLIENT_LIST_REQ, b"")


def encode_heartbeat() -> bytes:
    """Build a heartbeat (0x62) frame. Empty payload — automatic traffic a
    subscribed controller sends whenever nothing else went out recently, so
    the machine (and the WiFi module's own idle timer) keeps seeing this
    link as live. See ``machine/heartbeat.py`` for the timing decision."""
    return build_frame(PTYPE_HEARTBEAT, b"")


def encode_control_release() -> bytes:
    """Build a control-release (0x66) frame. Empty payload — a deliberate
    "give up control" sent only by whoever currently holds it. The machine
    only honours this from the current holder and only once multi-user mode
    is on; sent from anyone else, or in single-user mode, it does nothing."""
    return build_frame(PTYPE_CTRL_RELEASE, b"")


def encode_automatic_command(kind: int, data: bytes) -> bytes:
    """Build an automatic-command (0x6B) frame wrapping a connect-time query.

    ``kind``: 0 = console command (as CTRL_MULTI/0xA2 would carry), 1 =
    file-transfer start (as FILE_START/0xB0 would carry). ``data`` is
    normalised the same way ``encode_command``/``encode_file_command`` would
    normalise it for the channel it stands in for, so the wrapped payload is
    exactly the text that channel would otherwise carry — the machine only
    executes a wrapped command (never moving control to the sender) when its
    first word is on a fixed allow-list of read-only/self-contained
    commands; anything else wrapped this way is refused, not executed.
    """
    if kind == 0:
        payload = bytes(data).rstrip(b"\r\n")
    else:
        payload = bytes(data)
        if not payload.endswith(b"\n"):
            payload += b"\n"
    return build_frame(PTYPE_AUTO_COMMAND, bytes([kind & 0xFF]) + payload)


# File-transfer frames are owned by XMODEM while streamIO is paused. If any
# leak into the control parser, ignore them rather than treating as MDI text.
_FILE_TRANSFER_TYPES = frozenset(
    {
        PTYPE_FILE_START,
        PTYPE_FILE_MD5,
        PTYPE_FILE_VIEW,
        PTYPE_FILE_DATA,
        PTYPE_FILE_END,
        PTYPE_FILE_CAN,
        PTYPE_FILE_RETRY,
    }
)


class _RevPacketState(Enum):
    WAIT_HEADER = auto()
    READ_LENGTH = auto()
    READ_DATA = auto()
    CHECK_FOOTER = auto()


class MakeraProtocol(CommunicationProtocol):
    name = "makera"
    uses_framed_transfer = True

    def __init__(self) -> None:
        super().__init__()
        self._state = _RevPacketState.WAIT_HEADER
        self._packet_data = bytearray()
        self._header_buffer = bytearray(2)
        self._footer_buffer = bytearray(2)
        self._bytes_needed = 2
        self._expected_length = 0
        # PTYPE_NORMAL_INFO payloads are fragments of a text line, not
        # complete outputs. Buffer until a newline completes the line.
        self._normal_info_line = bytearray()
        # PTYPE_PUBLISHED_LINE fragments (0x69): buffered until a fragment
        # arrives with more=False. Every fragment carries its own
        # source_id/source_name (see PublishedLineFragment), so only the
        # text needs accumulating; _published_line_source is refreshed from
        # each fragment and used once the line completes. The protocol
        # contract guarantees fragments from one source are never
        # interleaved with another's, so one buffer (not one per source) is
        # enough.
        self._published_line_text = bytearray()
        self._published_line_source: tuple[int, str] | None = None

    def encode_command(self, data: bytes) -> bytes:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        # Match OEM Makera framing: CTRL_MULTI payloads are not newline-terminated.
        # A trailing \n breaks numeric parsers such as baud (strtol requires *end == '\0').
        payload = bytes(data).rstrip(b"\r\n")
        return build_frame(PTYPE_CTRL_MULTI, payload)

    def encode_realtime(self, char: int) -> bytes:
        return build_frame(PTYPE_CTRL_SINGLE, bytes([char & 0xFF]))

    def encode_file_command(self, data: bytes) -> bytes:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        payload = bytes(data)
        if not payload.endswith(b"\n"):
            payload += b"\n"
        return build_frame(PTYPE_FILE_START, payload)

    def feed(self, data: bytes) -> list[ParsedMessage]:
        messages: list[ParsedMessage] = []
        for byte in data:
            messages.extend(self._feed_byte(byte))
        return messages

    def _feed_byte(self, byte: int) -> list[ParsedMessage]:
        if self._state == _RevPacketState.WAIT_HEADER:
            self._header_buffer[0] = self._header_buffer[1]
            self._header_buffer[1] = byte
            checksum = (self._header_buffer[0] << 8) | self._header_buffer[1]
            if checksum == FRAME_HEADER:
                self._state = _RevPacketState.READ_LENGTH
                self._bytes_needed = 2
                self._packet_data.clear()
            return []

        if self._state == _RevPacketState.READ_LENGTH:
            self._packet_data.append(byte)
            self._bytes_needed -= 1
            if self._bytes_needed == 0:
                self._expected_length = (self._packet_data[0] << 8) | self._packet_data[1]
                if 0 <= self._expected_length <= MAX_FRAME_DATA_LENGTH:
                    self._state = _RevPacketState.READ_DATA
                    self._bytes_needed = self._expected_length
                else:
                    self._state = _RevPacketState.WAIT_HEADER
            return []

        if self._state == _RevPacketState.READ_DATA:
            self._packet_data.append(byte)
            self._bytes_needed -= 1
            if self._bytes_needed == 0:
                self._state = _RevPacketState.CHECK_FOOTER
                self._bytes_needed = 2
            return []

        if self._state == _RevPacketState.CHECK_FOOTER:
            self._footer_buffer[0] = self._footer_buffer[1]
            self._footer_buffer[1] = byte
            self._bytes_needed -= 1
            if self._bytes_needed != 0:
                return []
            checksum = (self._footer_buffer[0] << 8) | self._footer_buffer[1]
            self._state = _RevPacketState.WAIT_HEADER
            if checksum != FRAME_END:
                self._packet_data.clear()
                return []
            return self._dispatch_packet()

        return []

    def _dispatch_packet(self) -> list[ParsedMessage]:
        parsed = validate_packet_data(self._packet_data)
        self._packet_data.clear()
        if parsed is None:
            return []
        # Any complete, CRC-valid frame — regardless of type — proves the
        # link is genuinely speaking the framed protocol.
        self.frame_confirmed = True

        if parsed.ptype in _FILE_TRANSFER_TYPES:
            return []

        # New multi-client types must be intercepted here, before the
        # unknown-type-becomes-console-LINE fallback below, or a new
        # controller talking to another new controller/firmware would show
        # raw protocol frames as garbled console text.
        if parsed.ptype == PTYPE_HELLO_ACK:
            return [ParsedMessage(MessageKind.HELLO_ACK, payload=parsed.payload)]
        if parsed.ptype == PTYPE_CLIENT_LIST_REPLY:
            return [ParsedMessage(MessageKind.CLIENT_LIST, payload=parsed.payload)]
        if parsed.ptype == PTYPE_PUBLISHED_LINE:
            return self._buffer_published_line(parsed.payload)
        if parsed.ptype == PTYPE_EVENT:
            # This `0x68` event type also carries kinds this controller
            # doesn't decode yet (upload finished, play started, job
            # ended, alarm/halt) — reserved for a future ticket.
            # Intercepted here, ahead of the unknown-type fallback below,
            # purely so it's never mistaken for garbled console text.
            return [ParsedMessage(MessageKind.EVENT, payload=parsed.payload)]

        if parsed.ptype == PTYPE_LOAD_FINISH:
            return [ParsedMessage(MessageKind.LOAD_EOF)]
        if parsed.ptype == PTYPE_LOAD_ERROR:
            return [ParsedMessage(MessageKind.LOAD_ERROR)]
        if parsed.ptype == PTYPE_NORMAL_INFO:
            return self._buffer_normal_info(parsed.payload)

        text = parsed.payload.decode(errors="ignore")
        if not text:
            return []

        # Status/diag always become LINE. LOAD_INFO chunks go to the
        # load buffer path; Controller decides via loadNUM.
        if parsed.ptype == PTYPE_LOAD_INFO:
            return [ParsedMessage(MessageKind.LOAD_CHUNK, text)]
        return [ParsedMessage(MessageKind.LINE, text)]

    def _buffer_normal_info(self, payload: bytes) -> list[ParsedMessage]:
        """Accumulate NORMAL_INFO fragments until a newline completes a line."""
        messages: list[ParsedMessage] = []
        for byte in payload:
            if byte == 0x0A:  # '\n' marks the end of one MDI/log line
                text = self._normal_info_line.decode(errors="ignore")
                self._normal_info_line.clear()
                messages.append(ParsedMessage(MessageKind.LINE, text))
            else:
                self._normal_info_line.append(byte)
        return messages

    def _buffer_published_line(self, payload: bytes) -> list[ParsedMessage]:
        """Accumulate PUBLISHED_LINE (0x69) fragments until one arrives with
        more=False, then emit one PUBLISHED_LINE message for the whole
        line. A malformed fragment (too short, name too long) is dropped
        silently, the same tolerant style the rest of this dispatcher uses
        for other wire messages.
        """
        fragment = decode_published_line(payload)
        if fragment is None:
            return []
        self._published_line_source = (fragment.source_id, fragment.source_name)
        self._published_line_text.extend(fragment.text)
        if fragment.more:
            return []
        text = self._published_line_text.decode(errors="ignore")
        self._published_line_text = bytearray()
        source_id, source_name = self._published_line_source
        self._published_line_source = None
        return [ParsedMessage(MessageKind.PUBLISHED_LINE, text=text, source_id=source_id, source_name=source_name)]

    def reset(self) -> None:
        self._state = _RevPacketState.WAIT_HEADER
        self._packet_data.clear()
        self._header_buffer = bytearray(2)
        self._footer_buffer = bytearray(2)
        self._bytes_needed = 2
        self._expected_length = 0
        self._normal_info_line.clear()
        self._published_line_text = bytearray()
        self._published_line_source = None
        self.ready = False
