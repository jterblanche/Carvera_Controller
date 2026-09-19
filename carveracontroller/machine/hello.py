"""Connect-time identify handshake: hello, ack, and the old-firmware fallback.

Pure state machine, no I/O: the caller supplies the current time and valid-
frame/ack/timeout events, and gets back the bytes to send (if any) and the
outcome. The rules this negotiates: a controller must not send anything to
the machine except realtime bytes and the hello itself until it has an
accepted hello ack; if no ack arrives within a short window, it falls back
to behaving as if the machine had never heard of hello at all; and a client
that hasn't identified itself keeps re-announcing itself for a while in case
its first hello or the machine's ack got lost on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

from ..protocols.handshake import HELLO_ACCEPTED, HelloAck
from ..protocols.makera import HELLO_PROTOCOL_VERSION, encode_hello
from .identity import ControllerIdentity

# Controller-side wait for an accepted hello ack before falling back to
# legacy (pre-identify) behaviour. This is the controller's own decision,
# distinct from the machine's own hello window (below): an accepted ack
# measures about 70 ms round trip in practice, so 1.0 s is ample margin
# without visibly slowing every connect to firmware that never answers at
# all.
ACK_TIMEOUT_S = 1.0

# How long a machine that understands hello is expected to still be
# listening for one after a client connects, before it gives up and treats
# that client as an old, non-identifying one. Re-hello attempts (below) stop
# once this has passed — there is no point announcing to a machine that has
# already decided this link is not going to identify itself.
HELLO_WINDOW_S = 5.0


class Resolution(Enum):
    """How the handshake ended, once it has ended."""

    IDENTIFIED = auto()
    REJECTED = auto()
    FALLBACK = auto()


@dataclass
class HelloNegotiator:
    """Tracks one connection's identify handshake.

    ``applicable`` is False for a link that isn't speaking the framed
    protocol at all (e.g. a legacy plaintext line protocol) — hello must
    never be sent there, since raw framed bytes (CRC, random id) are not
    ASCII-constrained and could be misread as control characters by a
    plaintext parser. Such a negotiator resolves to FALLBACK immediately and
    never sends anything.
    """

    identity: ControllerIdentity
    link: int
    applicable: bool = True
    # When the *first* hello was sent. The ack-wait deadline is anchored
    # here and never moves — see _last_hello_sent_at below for why that
    # matters.
    _first_hello_sent_at: float | None = field(default=None, init=False, repr=False)
    # When the most recent hello (first or re-hello) was sent. Deliberately
    # a separate field from _first_hello_sent_at: if re-hello (on_status_reply)
    # refreshed the same timestamp poll() times out against, a steady stream
    # of status replies would keep re-triggering it and the 1.0s fallback
    # would never actually fire against old firmware.
    _last_hello_sent_at: float | None = field(default=None, init=False, repr=False)
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
        if not self.applicable or self._first_hello_sent_at is not None:
            return None
        return self._send_hello(now, first=True)

    def on_status_reply(self, now: float) -> bytes | None:
        """Call whenever a status reply arrives (proof the link is live).

        Re-sends hello if still unidentified, the previous hello is stale,
        and the machine's own hello window (since the first hello) hasn't
        passed yet — past that point the machine has already given up on
        this link identifying itself, so a further hello would go nowhere.
        Does not affect ``resolution``: a late ack after FALLBACK can still
        identify the controller for anything that checks ``identified``.
        """
        if not self.applicable or self._identified or self._last_hello_sent_at is None:
            return None
        if self._first_hello_sent_at is not None and now - self._first_hello_sent_at >= HELLO_WINDOW_S:
            return None
        if now - self._last_hello_sent_at < ACK_TIMEOUT_S:
            return None
        return self._send_hello(now, first=False)

    def poll(self, now: float) -> bool:
        """Call periodically. Returns True exactly once: the moment the ack
        window elapses with no ack, resolving to FALLBACK. Measured from the
        first hello sent, so a re-hello triggered by a live status reply
        cannot keep pushing this deadline back."""
        if self._resolution is not None or self._first_hello_sent_at is None:
            return False
        if now - self._first_hello_sent_at >= ACK_TIMEOUT_S:
            self._resolution = Resolution.FALLBACK
            return True
        return False

    def on_hello_ack(self, ack: HelloAck) -> bool:
        """Process a received hello ack. Returns True iff this ack newly
        identifies this controller — False for a duplicate ack on a link
        that was already identified, so a caller using this to trigger a
        one-off action (like requesting the client list) doesn't repeat it
        on every re-ack.

        An ack with an unrecognised ``protocol_version`` is ignored — the
        sender is treated as unidentified.
        """
        if ack.protocol_version != HELLO_PROTOCOL_VERSION:
            return False
        if ack.result == HELLO_ACCEPTED:
            was_identified = self._identified
            self._identified = True
            if self._resolution is None:
                self._resolution = Resolution.IDENTIFIED
            return not was_identified
        if self._resolution is None:
            self._resolution = Resolution.REJECTED
        return False

    def _send_hello(self, now: float, first: bool) -> bytes:
        if first:
            self._first_hello_sent_at = now
        self._last_hello_sent_at = now
        return encode_hello(self.identity.id, self.identity.name.encode("utf-8"), self.link)
