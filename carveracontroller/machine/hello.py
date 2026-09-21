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

# Default for how long the controller waits, from connection open, for the
# link to produce even one CRC-valid frame — the event ACK_TIMEOUT_S's own
# deadline is anchored on (on_valid_frame -> _first_hello_sent_at). Without
# this second anchor, a link that never yields a single valid frame never
# starts that deadline, so poll() never fires and every gated send queues
# forever (the bug this constant fixes: see the change explanation for the
# fake-machine scenario that exposed it).
#
# This default is sized for a link that was already live when this
# negotiator was constructed (WiFi, or bulk USB, neither of which reset the
# machine on open) — see the measurements below. It is NOT long enough for
# a USB-serial connect, which toggles DTR and resets the machine right
# before this negotiator is built: the board may still be mid-boot, which
# is exactly why Controller.open() already gives that case a much longer
# heartbeat grace (20 s) before an unrelated check would call the link
# dead. Controller.open() passes that same 20 s as this negotiator's
# ``open_timeout_s`` for a USB-serial connect, overriding this default,
# rather than risk flushing queued commands into a machine that hasn't
# finished booting yet.
#
# Firing this does NOT give up on hello for good: _advance_hello (Controller.py)
# keeps calling on_valid_frame every tick even after this resolves to
# FALLBACK, so a frame that arrives late still sends hello and can still
# identify the controller (the same "late ack after FALLBACK" behaviour
# ACK_TIMEOUT_S already has — see test_late_ack_after_fallback_still_identifies).
# This deadline only decides how long queued sends are held, not whether
# hello can still happen.
#
# Grounded in what's measured elsewhere in this repo, not a round number:
#   - a live machine's own first reply, over WiFi, has been observed
#     56-668 ms after connect (docs/testing/connection-follows-me/results/
#     2026-09-20_upload-per-client-461336c/report.md) — well under 1 s even
#     at the slow end seen so far.
#   - an accepted hello ack alone (a strict subset of "any valid frame",
#     since the ack is itself carried in one) measures ~70 ms round trip in
#     practice, the basis ACK_TIMEOUT_S's 1.0 s was chosen against.
#   - the protocol detector (protocols/detector.py) has already spent up to
#     PROBE_ATTEMPTS * (PROBE_WAIT_S send-wait + PROBE_WAIT_S read-timeout)
#     = 0.6 s *before* this negotiator even exists, probing for a plaintext
#     echo and finding none — that cost is already paid by connect time and
#     is not part of this window.
# 3.0 s is worth roughly 4.5x the slowest first reply measured so far,
# comfortably above the noise in that 56-668 ms spread, while staying well
# under the firmware's own 5.0 s HELLO_WINDOW_S patience — the firmware
# keeps listening for a hello for a full 5 s after connect, so stopping at
# 3.0 s (not 5.0 s) still leaves 2 s of headroom inside that window for a
# late first frame to still trigger hello, on top of not being a round
# number nobody could justify — and under the couple of seconds past which
# a user waiting on a command starts to suspect the app has hung.
OPEN_TIMEOUT_S = 3.0


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
    # When this connection was opened (the caller's time.monotonic() at the
    # point this negotiator was constructed). OPEN_TIMEOUT_S's deadline is
    # anchored here and never moves. Defaults to 0.0 so tests that only
    # care about relative timing (most of them) can omit it and just pass
    # small `now` values starting near zero, exactly as they already do for
    # on_valid_frame/poll/on_status_reply.
    opened_at: float = 0.0
    # How long, from opened_at, this negotiator waits for a first CRC-valid
    # frame before giving up on the handshake ever starting at all (see
    # ``poll``). None (the default) resolves to OPEN_TIMEOUT_S in
    # __post_init__ — the value grounded in measured WiFi/bulk-USB reply
    # latency above. A caller passes an explicit, larger value for a link
    # that may still be mid-boot when this negotiator is constructed (a
    # USB-serial connect after the DTR-triggered reset): OPEN_TIMEOUT_S is
    # far too short there — Controller.open() passes the same grace period
    # it already gives the unrelated heartbeat-drop check for exactly that
    # link, rather than inventing a second unjustified number.
    open_timeout_s: float | None = None
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
        if self.open_timeout_s is None:
            self.open_timeout_s = OPEN_TIMEOUT_S
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

    @property
    def frame_seen(self) -> bool:
        """Whether a CRC-valid frame has ever arrived on this link (i.e.
        whether on_valid_frame has fired and the ack-wait deadline has
        started). A caller can use this, checked just before calling
        poll(), to tell "this link answered but never acked" (the original,
        common fallback: old firmware) apart from "this link never answered
        at all" (the OPEN_TIMEOUT_S fallback: a broken or unresponsive
        machine) — the two are worth reporting differently.
        """
        return self._first_hello_sent_at is not None

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
        """Call periodically. Returns True exactly once: the moment either
        deadline below elapses unmet, resolving to FALLBACK.

        Two independent deadlines, checked in order:
          - the ack-wait deadline (ACK_TIMEOUT_S), measured from the first
            hello sent, once a valid frame has made that possible. A
            re-hello triggered by a live status reply cannot push this
            deadline back — see _last_hello_sent_at above.
          - the open-wait deadline (``open_timeout_s``, OPEN_TIMEOUT_S
            unless the caller overrode it), measured from connection open,
            for the case the first one can never even start: no valid frame
            has arrived at all, so no hello has been sent, so there is no
            ack to wait for. Without this, a link that never yields a
            single valid frame would hold every gated send forever.
        """
        if self._resolution is not None:
            return False
        if self._first_hello_sent_at is not None:
            if now - self._first_hello_sent_at >= ACK_TIMEOUT_S:
                self._resolution = Resolution.FALLBACK
                return True
            return False
        # __post_init__ always resolves None to OPEN_TIMEOUT_S, so this is
        # always a float by the time poll() can run; the assert is only to
        # satisfy the type checker across that dataclass-field boundary.
        assert self.open_timeout_s is not None
        if now - self.opened_at >= self.open_timeout_s:
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
