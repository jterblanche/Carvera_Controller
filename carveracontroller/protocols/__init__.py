"""Extensible machine communication protocols."""

from .base import CommunicationProtocol
from .detector import detect_protocol_name
from .handshake import (
    EVENT_KIND_CONTROL_CHANGED,
    EVENT_KIND_PLAY_STARTED,
    EVENT_KIND_UPLOAD_FINISHED,
    HELLO_ACCEPTED,
    HELLO_REJECTED_CAP,
    HELLO_REJECTED_OLD_CONTROLLER,
    LINK_USB,
    LINK_WIFI,
    ClientEntry,
    ControlChanged,
    HelloAck,
    PlayStarted,
    PublishedLineFragment,
    UploadFinished,
    decode_client_list,
    decode_control_changed_event,
    decode_hello_ack,
    decode_play_started_event,
    decode_published_line,
    decode_upload_finished_event,
)
from .makera import encode_automatic_command, encode_client_list_request, encode_heartbeat, encode_hello, encode_relay
from .messages import MessageKind, ParsedMessage
from .registry import (
    DEFAULT_PROTOCOL,
    available_protocols,
    create_protocol,
    register_protocol,
)
from .relay import RELAY_KIND_TOOL_TABLE, decode_tool_table_relay, encode_tool_table_relay
from .session import ProtocolSession, protocol_name_from_announcement

__all__ = [
    "CommunicationProtocol",
    "DEFAULT_PROTOCOL",
    "EVENT_KIND_CONTROL_CHANGED",
    "EVENT_KIND_PLAY_STARTED",
    "EVENT_KIND_UPLOAD_FINISHED",
    "HELLO_ACCEPTED",
    "HELLO_REJECTED_CAP",
    "HELLO_REJECTED_OLD_CONTROLLER",
    "LINK_USB",
    "LINK_WIFI",
    "RELAY_KIND_TOOL_TABLE",
    "ClientEntry",
    "ControlChanged",
    "HelloAck",
    "MessageKind",
    "ParsedMessage",
    "PlayStarted",
    "ProtocolSession",
    "PublishedLineFragment",
    "UploadFinished",
    "available_protocols",
    "create_protocol",
    "decode_client_list",
    "decode_control_changed_event",
    "decode_hello_ack",
    "decode_play_started_event",
    "decode_published_line",
    "decode_tool_table_relay",
    "decode_upload_finished_event",
    "detect_protocol_name",
    "encode_automatic_command",
    "encode_client_list_request",
    "encode_heartbeat",
    "encode_hello",
    "encode_relay",
    "encode_tool_table_relay",
    "protocol_name_from_announcement",
    "register_protocol",
]
