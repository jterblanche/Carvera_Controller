"""What this controller does while the machine holds a job's start.

A machine that holds starts (bit 0 of the hello ack's features byte) does
not start a job at once while other controllers that take part are
connected. It announces the job with a job-start event (``0x68`` kind 8)
in phase "waiting", once when the hold begins and then once a second, and
starts when every listed controller has said it is ready (frame ``0x6C``),
when its time limit runs out, or when the starting controller sends
``start-now``. The starting controller can also cancel with ``abort``, and
the hold is cancelled if it disconnects or the machine halts. The event
then says "starting" or "cancelled" once.

This tracker turns those events into what this controller has to do:

- prepare (draw the announced file from a local copy, or fetch it) the
  first time a waiting event lists this controller as not ready;
- nothing while that is under way;
- prepare again after a failed attempt (a download that failed, or a copy
  whose MD5 is not the announced one), once a backoff has passed and only
  while enough of the machine's time limit is left for another attempt;
- send ready again when a later waiting event for the same start still
  lists it, because a ready can be lost (frames from other WiFi
  controllers are dropped while one controller's transfer runs);
- nothing when it is the starter, which is never waited for.

It also keeps the countdown: the machine's own seconds left, counted down
locally between events, because no event is sent while a file transfer
runs. A hold whose events stop altogether (a lost "starting" or
"cancelled") is dropped once it is well past its limit.

Pure, no I/O: the caller passes the decoded events and the current time
(time.monotonic()), and does the drawing, fetching and sending itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto

from ..protocols.handshake import JOB_START_WAITING, JobStartEvent

# The wait after a failed attempt before the next one: doubled after each
# failure of the same start, up to the maximum.
RETRY_BACKOFF_S = 2.0
MAX_RETRY_BACKOFF_S = 8.0

# No new attempt starts with fewer seconds than this left to the machine's
# limit: a download, its drawing and the ready would not finish in time.
MIN_SECONDS_LEFT_TO_RETRY = 3

# How long past the machine's limit a hold is still shown with no further
# event. The start can come later than the limit only while a download is
# running on the machine, and a stalled download is ended after 10 s.
STALE_AFTER_LIMIT_S = 15.0


class JobStartAction(Enum):
    """What a waiting event asks of this controller."""

    NONE = auto()
    PREPARE = auto()
    RESEND_READY = auto()


@dataclass
class JobStartTracker:
    """One instance per connection. ``own_id`` is this controller's id, as
    sent in its hello."""

    own_id: int
    _event: JobStartEvent | None = field(default=None, init=False, repr=False)
    _deadline: float = field(default=0.0, init=False, repr=False)
    _preparing_for: int = field(default=0, init=False, repr=False)
    _ready_for: int = field(default=0, init=False, repr=False)
    _failures: int = field(default=0, init=False, repr=False)
    _retry_after: float = field(default=0.0, init=False, repr=False)

    @property
    def held(self) -> bool:
        """True while a start is held, as far as this controller knows."""
        return self._event is not None

    @property
    def event(self) -> JobStartEvent | None:
        """The latest waiting event of the held start, or None."""
        return self._event

    @property
    def ready_sent(self) -> bool:
        """True while a start is held for which this controller has sent
        ready."""
        return self._event is not None and self._ready_for == self._event.start_id

    @property
    def is_starter(self) -> bool:
        """True while a start is held that this controller started."""
        return self._event is not None and self._event.starter_id == self.own_id

    def on_event(self, event: JobStartEvent, now: float) -> JobStartAction:
        """Take a job-start event. A "starting" or "cancelled" event ends
        the hold. A waiting event updates the countdown and says what to do
        (see the module docstring)."""
        if event.phase != JOB_START_WAITING:
            self.clear()
            return JobStartAction.NONE
        if self._event is None or self._event.start_id != event.start_id:
            self._preparing_for = 0
            self._ready_for = 0
            self._failures = 0
            self._retry_after = 0.0
        self._event = event
        self._deadline = now + event.seconds_left
        if event.starter_id == self.own_id or self.own_id not in event.not_ready_ids:
            return JobStartAction.NONE
        if self._ready_for == event.start_id:
            return JobStartAction.RESEND_READY
        if self._preparing_for == event.start_id:
            return JobStartAction.NONE
        if self._failures and (now < self._retry_after or event.seconds_left < MIN_SECONDS_LEFT_TO_RETRY):
            return JobStartAction.NONE
        return JobStartAction.PREPARE

    def begin_preparing(self, start_id: int) -> None:
        """The caller has started drawing or fetching the file for
        `start_id`: later waiting events ask for nothing until
        ``prepared``."""
        self._preparing_for = start_id

    def prepared(self, start_id: int) -> bool:
        """The caller has drawn the file for `start_id` from a copy whose
        size and MD5 are the announced ones. Returns True if a ready should
        be sent now: the start is still the one held. Later waiting events
        that still list this controller ask for the ready again."""
        if self._preparing_for == start_id:
            self._preparing_for = 0
        if self._event is None or self._event.start_id != start_id:
            return False
        self._ready_for = start_id
        return True

    def failed(self, start_id: int, now: float) -> None:
        """The attempt for `start_id` did not give a good copy. A later
        waiting event asks for another attempt once the backoff has passed
        (see on_event); no ready is sent."""
        if self._preparing_for == start_id:
            self._preparing_for = 0
        if self._event is None or self._event.start_id != start_id:
            return
        self._failures += 1
        backoff = min(RETRY_BACKOFF_S * 2 ** (self._failures - 1), MAX_RETRY_BACKOFF_S)
        self._retry_after = now + backoff

    def give_up(self, start_id: int) -> None:
        """No attempt for `start_id` can give a good copy (the machine
        announced no MD5 to check one against): ask for nothing more, and
        send no ready."""
        if self._preparing_for == start_id:
            self._preparing_for = 0
        if self._event is None or self._event.start_id != start_id:
            return
        self._failures += 1
        self._retry_after = math.inf

    def seconds_left(self, now: float) -> int:
        """Whole seconds to the machine's limit, rounded up, never below 0."""
        if self._event is None:
            return 0
        return max(0, math.ceil(self._deadline - now))

    def stale(self, now: float) -> bool:
        """True when a held start has had no event for well past its limit,
        so its "starting" or "cancelled" event was lost."""
        return self._event is not None and now > self._deadline + STALE_AFTER_LIMIT_S

    def clear(self) -> None:
        """Forget the held start (it started, was cancelled, or the
        connection closed)."""
        self._event = None
        self._deadline = 0.0
        self._preparing_for = 0
        self._ready_for = 0
        self._failures = 0
        self._retry_after = 0.0
