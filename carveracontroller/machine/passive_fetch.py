"""When a passive controller should fetch and draw the job file it did not
select itself.

A passive controller (one that is not the current control holder) learns
the job's path from the machine's own upload-finished/play-started events,
not from anything it did locally. Fetching and drawing a G-code file is not
free (a card read plus parsing thousands of lines), so this waits for the
machine to be idle before doing it -- exactly like the connect-time config
download already does. If play starts before the machine goes idle, the
controller shows progress, time and position only (from the published
status, which needs no file) and fetches once the machine is idle again.

"Idle" here means the player has stopped, not just the state word: the
firmware reports Idle whenever its motion queue is empty, which also
happens mid-job (a tool change, a probe, the start-of-job routine). Where
the status report carries the player's playing flag (the fourth value of
its P: field), a fetch is released only after SETTLE_SAMPLES consecutive
observations of Idle with the flag at 0. Where it does not, the state
word decides on its own, on the first Idle report.

Pure decision logic, no I/O: the caller (main.py) owns actually fetching
the file, drawing it, and reading the machine's current Idle-ness from its
own status -- this only tracks *which* path is owed a fetch and *when* it
is due. Kept Kivy-free so it is testable without any UI, the same reason
carveracontroller/machine/hello.py and heartbeat.py are. Timing (the backoff
between retries) is injected via a ``now`` the caller passes in, the same
style as heartbeat.py's ``heartbeat_due`` -- never read from a clock in
here, so it stays deterministic to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Minimum time between automatic retries of the same failed fetch, so a
# persistently failing download (a dropped link, a file the machine no
# longer has) does not retry on every idle status tick.
RETRY_BACKOFF_S = 10.0

# Consecutive failures of the same path before giving up on it
# automatically. A fresh upload-finished/play-started event -- for this
# path or a different one -- resets the count, since that is a new ask
# from the machine, not the same stuck one.
MAX_FETCH_ATTEMPTS = 3

# Consecutive due_fetch calls that must see the machine Idle with the
# player stopped before a fetch is released, when the firmware reports the
# player flag. More than one, because the call made as the
# upload-finished/play-started event arrives sees the status report taken
# before the event. The count restarts at every event, so at least one
# report taken after the event has to agree.
SETTLE_SAMPLES = 2


def player_flag(is_playing: int, reports_flag: bool) -> bool | None:
    """The player's playing flag as due_fetch takes it. `is_playing` is the
    parsed fourth value of the status report's P: field, which reads 0 both
    when the player is stopped and when the field is absent. `reports_flag`
    says whether this firmware is known to report the field. A 1 can only
    come from firmware that reports it, so it always means playing; a 0
    means stopped only when the firmware reports the field, and otherwise
    says nothing (None)."""
    if is_playing == 1:
        return True
    return False if reports_flag else None


@dataclass
class PassiveFetchTracker:
    """One instance per connection. ``note_published_file`` records a path
    a published event just named; ``due_fetch`` returns it once the machine
    is next observed idle. A path already loaded (``mark_loaded``) is not
    re-queued, so re-observing the same play-started event (or an
    upload-finished immediately followed by a play-started for the same
    file) does not trigger a second fetch. ``note_fetch_failed`` is the
    counterpart for a fetch that did not pan out: it keeps the path pending
    for a later retry, but backs off so the retry does not happen on the
    very next tick, and stops retrying automatically after
    ``MAX_FETCH_ATTEMPTS``.
    """

    _pending_path: str | None = field(default=None, init=False, repr=False)
    _loaded_path: str | None = field(default=None, init=False, repr=False)
    _retry_after: float | None = field(default=None, init=False, repr=False)
    _failing_path: str | None = field(default=None, init=False, repr=False)
    _fail_count: int = field(default=0, init=False, repr=False)
    _settled_samples: int = field(default=0, init=False, repr=False)

    @property
    def pending_path(self) -> str | None:
        """The path waiting for the machine to go idle, or None."""
        return self._pending_path

    def note_published_file(self, path: str) -> None:
        """Call on an upload-finished or play-started event naming `path`.
        A blank path is ignored (malformed event, nothing to fetch); a path
        already loaded is not re-queued. Always treated as a fresh ask, so
        it resets any backoff/attempt count a previous failed fetch of this
        (or another) path had built up, and the count of settled
        observations (SETTLE_SAMPLES)."""
        if path and path != self._loaded_path:
            self._pending_path = path
            self._settled_samples = 0
            self._retry_after = None
            self._failing_path = None
            self._fail_count = 0

    def due_fetch(self, is_idle: bool, now: float | None = None, playing: bool | None = None) -> str | None:
        """Call whenever the machine's Idle-ness is (re)observed, e.g. on
        every status update. Returns the path to fetch right now, clearing
        the pending request -- or None if nothing is pending, the machine
        is not settled, or a previous failure's backoff has not elapsed yet
        (``now``, required to check backoff; omit only when nothing can be
        backing off, e.g. in tests that never call ``note_fetch_failed``).
        ``playing`` is the player flag from the same status report (see
        ``player_flag``), or None when the firmware does not report it.
        With a flag, the machine is settled once SETTLE_SAMPLES consecutive
        calls since the last published event saw it Idle and not playing;
        without one, on any call that sees it Idle.
        The caller should call ``mark_loaded`` once the fetch actually
        completes, or ``note_fetch_failed`` if it does not -- never nothing,
        or a slow fetch could be retriggered while still in flight."""
        if playing is None:
            settled = is_idle
        else:
            self._settled_samples = self._settled_samples + 1 if is_idle and not playing else 0
            settled = self._settled_samples >= SETTLE_SAMPLES
        if not settled or self._pending_path is None:
            return None
        if self._retry_after is not None and (now is None or now < self._retry_after):
            return None
        path = self._pending_path
        self._pending_path = None
        return path

    def note_fetch_failed(self, path: str, now: float) -> None:
        """Call instead of ``mark_loaded`` when a fetch for `path` (started
        via ``due_fetch``) did not complete. Leaves it pending so a later
        idle status tick retries it, but not before ``now + RETRY_BACKOFF_S``
        -- and, after ``MAX_FETCH_ATTEMPTS`` consecutive failures for this
        same path, stops re-queuing it at all, until a fresh
        ``note_published_file`` (for this path or another) asks again."""
        if path == self._failing_path:
            self._fail_count += 1
        else:
            self._failing_path = path
            self._fail_count = 1
        if self._fail_count >= MAX_FETCH_ATTEMPTS:
            self._pending_path = None
            self._retry_after = None
            return
        self._pending_path = path
        self._retry_after = now + RETRY_BACKOFF_S

    def mark_loaded(self, path: str) -> None:
        """Call once `path` is actually loaded and drawn -- whether that
        fetch was triggered by `due_fetch` or the file was loaded some
        other way (this controller started the job itself, or the operator
        opened it manually). Clears any pending request for the same or an
        older path, and any backoff state."""
        self._loaded_path = path
        self._pending_path = None
        self._retry_after = None
        self._failing_path = None
        self._fail_count = 0
