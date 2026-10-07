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

from ..protocols.handshake import (
    HELLO_ACCEPTED,
    HELLO_BUSY,
    HELLO_FEATURE_JOB_START_WAIT,
    HELLO_MODE_SINGLE_USER,
    HELLO_REJECTED_IDENTITY_CONNECTED,
    HelloAck,
)
from ..protocols.makera import HELLO_PROTOCOL_VERSION, encode_hello
from .identity import ControllerIdentity

# Controller-side wait for a hello ack before falling back to legacy
# (pre-identify) behaviour. This is the controller's own decision, distinct
# from the machine's own hello window (below). An ack normally measures about
# 70 ms round trip, but when this id is already connected from a different
# launch the machine holds the hello for up to 2 s while it asks the other
# connection whether it is still there, so the wait is 3 s. Firmware that
# never answers hello at all is the only case that waits the full time.
ACK_TIMEOUT_S = 3.0

# How long after the last hello a still-unidentified controller on a live
# link sends it again, in case that hello or its ack was lost.
REHELLO_INTERVAL_S = 1.0

# Result 4 ("busy"): the machine checks one hello at a time and has not
# looked at this one. The same hello goes again this long after each busy
# answer, without telling the user, until the machine answers otherwise or
# BUSY_GIVE_UP_S has passed since the first busy answer, when the controller
# gives up and says the machine is busy. Read at call time, so tests can
# shorten them.
BUSY_RETRY_S = 1.0
BUSY_GIVE_UP_S = 30.0

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
# forever. That is the bug this constant fixes; it was found by pointing the
# controller at a machine that accepts the connection and then sends nothing
# a frame decoder would accept.
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
# Grounded in measurement, not a round number:
#   - a live machine's own first reply, over WiFi, has been observed
#     between 56 ms and 668 ms after connect — well under 1 s even at the
#     slow end seen so far.
#   - an accepted hello ack alone (a strict subset of "any valid frame",
#     since the ack is itself carried in one) measures ~70 ms round trip in
#     practice when the machine does not hold the hello.
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
    # here (_ack_wait_from) and moves only when the hello is sent again
    # after a busy answer — see _last_hello_sent_at below for why a re-hello
    # must not move it.
    _first_hello_sent_at: float | None = field(default=None, init=False, repr=False)
    # When the most recent hello (first or re-hello) was sent. Deliberately
    # a separate field from _first_hello_sent_at: if re-hello (on_status_reply)
    # refreshed the same timestamp poll() times out against, a steady stream
    # of status replies would keep re-triggering it and the ack-wait
    # fallback would never actually fire against old firmware.
    _last_hello_sent_at: float | None = field(default=None, init=False, repr=False)
    _resolution: Resolution | None = field(default=None, init=False)
    _identified: bool = field(default=False, init=False)
    # Whether the machine reports single-user or multi-user control, set
    # from an accepted ack's own `mode` byte (see on_hello_ack below).
    # Stays HELLO_MODE_SINGLE_USER until an ack says otherwise -- the same
    # starting point as `identified`, and the only value old firmware (which
    # never sends an ack at all) ever has.
    _mode: int = field(default=HELLO_MODE_SINGLE_USER, init=False, repr=False)
    # The features byte of the last accepted ack (0 from firmware that sends
    # a three-byte ack, and before any ack); see machine_holds_starts.
    _ack_features: int = field(default=0, init=False, repr=False)
    # When the ack wait runs from: the first hello, then each hello re-sent
    # after a busy answer (the machine answers that one afresh).
    _ack_wait_from: float | None = field(default=None, init=False, repr=False)
    # When the machine's hello window runs from, for re-hello: the first
    # hello, then each busy answer (the machine restarts its window then).
    _window_from: float | None = field(default=None, init=False, repr=False)
    # The first busy answer of the current run of them, and when the hello is
    # next due again because of one (None when no retry is pending).
    _busy_since: float | None = field(default=None, init=False, repr=False)
    _busy_retry_at: float | None = field(default=None, init=False, repr=False)
    # The hello-ack result that ended this connection, once one has; see
    # ``refusal``.
    _refusal: int | None = field(default=None, init=False, repr=False)

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
    def mode(self) -> int:
        return self._mode

    @property
    def refusal(self) -> int | None:
        """The hello-ack result the machine refused this controller with, or
        HELLO_BUSY when it stayed busy for BUSY_GIVE_UP_S. None while not
        refused. Results 1 and 2 count only while the handshake is
        unresolved, as before; result 3 counts whenever this controller is
        not identified, because the machine has refused it even if the ack
        wait already ran out."""
        return self._refusal

    @property
    def machine_holds_starts(self) -> bool:
        """True once an accepted ack says the machine holds a job's start
        until the controllers that take part are ready. False for firmware
        without that wait, which never sends a job-start event."""
        return self._identified and bool(self._ack_features & HELLO_FEATURE_JOB_START_WAIT)

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
        if self._refusal is not None or self._busy_retry_at is not None:
            return None
        if self._window_from is not None and now - self._window_from >= HELLO_WINDOW_S:
            return None
        if now - self._last_hello_sent_at < REHELLO_INTERVAL_S:
            return None
        return self._send_hello(now, first=False)

    def due_hello(self, now: float) -> bytes | None:
        """Call periodically. Returns the same hello again once BUSY_RETRY_S
        has passed since a busy answer (result 4); None otherwise. Each busy
        answer gives one retry."""
        if self._busy_retry_at is None or self._identified or self._refusal is not None:
            return None
        if now < self._busy_retry_at:
            return None
        self._busy_retry_at = None
        self._ack_wait_from = now
        return self._send_hello(now, first=False)

    def poll(self, now: float) -> bool:
        """Call periodically. Returns True exactly once: the moment either
        deadline below elapses unmet, resolving to FALLBACK.

        Two independent deadlines, checked in order:
          - the ack-wait deadline (ACK_TIMEOUT_S), measured from the first
            hello sent, once a valid frame has made that possible, or from
            the hello last sent again after a busy answer. A re-hello
            triggered by a live status reply cannot push this deadline
            back — see _last_hello_sent_at above. It does not run while a
            retry after a busy answer is pending.
          - the open-wait deadline (``open_timeout_s``, OPEN_TIMEOUT_S
            unless the caller overrode it), measured from connection open,
            for the case the first one can never even start: no valid frame
            has arrived at all, so no hello has been sent, so there is no
            ack to wait for. Without this, a link that never yields a
            single valid frame would hold every gated send forever.
        """
        if self._resolution is not None:
            return False
        if self._busy_retry_at is not None:
            # Waiting to send the hello again after a busy answer: the
            # machine is answering, so this is not old firmware.
            return False
        if self._ack_wait_from is not None:
            if now - self._ack_wait_from >= ACK_TIMEOUT_S:
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

    def on_hello_ack(self, ack: HelloAck, now: float | None = None) -> bool:
        """Process a received hello ack. Returns True iff this ack newly
        identifies this controller — False for a duplicate ack on a link
        that was already identified, so a caller using this to trigger a
        one-off action (like requesting the client list) doesn't repeat it
        on every re-ack.

        An ack with an unrecognised ``protocol_version`` is ignored — the
        sender is treated as unidentified. ``now`` times a busy answer's
        retry; without it, the time of the last hello sent is used.

        A busy answer (result 4) resolves nothing: the same hello is due
        again BUSY_RETRY_S later (see ``due_hello``), until BUSY_GIVE_UP_S
        after the first busy answer, when this gives up with ``refusal`` set
        to HELLO_BUSY.
        """
        if ack.protocol_version != HELLO_PROTOCOL_VERSION:
            return False
        if ack.result == HELLO_ACCEPTED:
            was_identified = self._identified
            self._identified = True
            self._mode = ack.mode
            self._ack_features = ack.features
            self._busy_since = None
            self._busy_retry_at = None
            if self._resolution is None:
                self._resolution = Resolution.IDENTIFIED
            return not was_identified
        if self._identified or self._refusal is not None:
            return False
        if ack.result == HELLO_BUSY:
            if now is None:
                now = self._last_hello_sent_at or 0.0
            if self._busy_since is None:
                self._busy_since = now
            if now - self._busy_since >= BUSY_GIVE_UP_S:
                self._refuse(HELLO_BUSY)
                return False
            self._busy_retry_at = now + BUSY_RETRY_S
            self._window_from = now
            return False
        if self._resolution is None:
            self._refuse(ack.result)
        elif ack.result == HELLO_REJECTED_IDENTITY_CONNECTED:
            self._refusal = ack.result
        return False

    def _refuse(self, result: int) -> None:
        self._refusal = result
        self._busy_retry_at = None
        if self._resolution is None:
            self._resolution = Resolution.REJECTED

    def _send_hello(self, now: float, first: bool) -> bytes:
        if first:
            self._first_hello_sent_at = now
            self._ack_wait_from = now
            self._window_from = now
        self._last_hello_sent_at = now
        # Always says this controller takes part in the job-start wait:
        # firmware without the wait ignores the byte. The launch part is the
        # same in every hello of this start; firmware without the identity
        # check ignores it.
        return encode_hello(
            self.identity.id,
            self.identity.name.encode("utf-8"),
            self.link,
            features=HELLO_FEATURE_JOB_START_WAIT,
            launch=self.identity.launch,
        )
