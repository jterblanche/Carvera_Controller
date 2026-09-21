"""Extensible machine communication protocols."""

from .base import CommunicationProtocol
from .detector import detect_protocol_name
from .handshake import (
    HELLO_ACCEPTED,
    HELLO_REJECTED_CAP,
    HELLO_REJECTED_OLD_CONTROLLER,
    LINK_USB,
    LINK_WIFI,
    ClientEntry,
    HelloAck,
    PublishedLineFragment,
    decode_client_list,
    decode_hello_ack,
    decode_published_line,
)
from .makera import encode_automatic_command, encode_client_list_request, encode_heartbeat, encode_hello
from .messages import MessageKind, ParsedMessage
from .registry import (
    DEFAULT_PROTOCOL,
    available_protocols,
    create_protocol,
    register_protocol,
)
from .session import ProtocolSession, protocol_name_from_announcement

__all__ = [
    "CommunicationProtocol",
    "DEFAULT_PROTOCOL",
    "HELLO_ACCEPTED",
    "HELLO_REJECTED_CAP",
    "HELLO_REJECTED_OLD_CONTROLLER",
    "LINK_USB",
    "LINK_WIFI",
    "ClientEntry",
    "HelloAck",
    "MessageKind",
    "ParsedMessage",
    "ProtocolSession",
    "PublishedLineFragment",
    "available_protocols",
    "create_protocol",
    "decode_client_list",
    "decode_hello_ack",
    "decode_published_line",
    "detect_protocol_name",
    "encode_automatic_command",
    "encode_client_list_request",
    "encode_heartbeat",
    "encode_hello",
    "protocol_name_from_announcement",
    "register_protocol",
]
