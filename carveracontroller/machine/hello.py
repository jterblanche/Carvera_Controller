"""Connect-time identify handshake: hello, ack, and the old-firmware fallback.

Pure state machine, no I/O: the caller supplies the current time and valid-
frame/ack/timeout events, and gets back the bytes to send (if any) and the
outcome. See docs/protocol/connection-follows-me.md (workspace repo, not
part of this fork) §3-§5.2 for the wire contract this negotiates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

from ..protocols.handshake import HELLO_ACCEPTED, HelloAck
from ..protocols.makera import HELLO_PROTOCOL_VERSION, encode_hello
from .identity import ControllerIdentity

# Controller-side wait for an accepted hello ack before falling back to
# legacy (pre-identify) behaviour. This is the controller's own decision,
# distinct from the firmware's 5 s hello window: an accepted ack measures
# about 70 ms round trip in practice, so 1.0 s is ample margin without
# visibly slowing every connect to firmware that never answers at all.
ACK_TIMEOUT_S = 1.0


class Resolution(Enum):
    """How the handshake ended, once it has ended."""

    IDENTIFIED = auto()
    REJECTED = auto()
    FALLBACK = auto()


@dataclass
class HelloNegotiator:
    """Tracks one connection's identify handshake.

    ``applicable`` is False for a link that isn't speaking the Makera framed
    protocol at all (Smoothie mode) — hello must never be sent there
    (protocol doc §3), so such a negotiator resolves to FALLBACK immediately
    and never sends anything.
    """

    identity: ControllerIdentity
    link: int
    applicable: bool = True
    _hello_sent_at: float | None = field(default=None, init=False, repr=False)
    _resolution: Resolution | None = field(default=None, init=False)
    _identified: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.applicable:
            self._resolution = Resolution.FALLBACK

    @property
    def resolved(self) -> bool:
        return self._resolution is not None

    @property
    def resolution(self) -> Resolution | None:
        return self._resolution

    @property
    def identified(self) -> bool:
        return self._identified

    def on_valid_frame(self, now: float) -> bytes | None:
        """Call once the link has produced its first CRC-valid frame.

        Returns the hello frame to send the first time this applies; None on
        every later call (idempotent), or when hello does not apply here.
        """
        if not self.applicable or self._hello_sent_at is not None:
            return None
        return self._send_hello(now)

    def on_status_reply(self, now: float) -> bytes | None:
        """Call whenever a status reply arrives (proof the link is live).

        Re-sends hello if still unidentified and the previous hello is
        stale — the protocol doc's re-hello rule for a lost ack (§4.2).
        Does not affect ``resolution``: a late ack after FALLBACK can still
        identify the controller for anything that checks ``identified``.
        """
        if not self.applicable or self._identified or self._hello_sent_at is None:
            return None
        if now - self._hello_sent_at < ACK_TIMEOUT_S:
            return None
        return self._send_hello(now)

    def poll(self, now: float) -> bool:
        """Call periodically. Returns True exactly once: the moment the ack
        window elapses with no ack, resolving to FALLBACK."""
        if self._resolution is not None or self._hello_sent_at is None:
            return False
        if now - self._hello_sent_at >= ACK_TIMEOUT_S:
            self._resolution = Resolution.FALLBACK
            return True
        return False

    def on_hello_ack(self, ack: HelloAck) -> bool:
        """Process a received hello ack. Returns True iff it newly identifies
        this controller.

        An ack with an unrecognised ``protocol_version`` is ignored — the
        sender is treated as unidentified (protocol doc §10).
        """
        if ack.protocol_version != HELLO_PROTOCOL_VERSION:
            return False
        if ack.result == HELLO_ACCEPTED:
            self._identified = True
            if self._resolution is None:
                self._resolution = Resolution.IDENTIFIED
            return True
        if self._resolution is None:
            self._resolution = Resolution.REJECTED
        return False

    def _send_hello(self, now: float) -> bytes:
        self._hello_sent_at = now
        return encode_hello(self.identity.id, self.identity.name.encode("utf-8"), self.link)
