"""When a passive controller should fetch and draw the job file it did not
select itself.

A passive controller (one that is not the current control holder) learns
the job's path from the machine's own upload-finished/play-started events,
not from anything it did locally. Fetching and drawing a G-code file is not
free (a card read plus parsing thousands of lines), so this waits for the
machine to be idle before doing it -- exactly like the connect-time config
download already does, and named directly by the design this ticket
implements: "if play starts before the machine goes idle, it shows
progress, time and position only [from the published status, which needs
no file] and fetches when the machine is idle again."

Pure decision logic, no I/O: the caller (main.py) owns actually fetching
the file, drawing it, and reading the machine's current Idle-ness from its
own status -- this only tracks *which* path is owed a fetch and *when* it
is due. Kept Kivy-free so it is testable without any UI, the same reason
carveracontroller/machine/hello.py and heartbeat.py are.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PassiveFetchTracker:
    """One instance per connection. ``note_published_file`` records a path
    a published event just named; ``due_fetch`` returns it once the machine
    is next observed idle. A path already loaded (``mark_loaded``) is not
    re-queued, so re-observing the same play-started event (or an
    upload-finished immediately followed by a play-started for the same
    file) does not trigger a second fetch.
    """

    _pending_path: str | None = field(default=None, init=False, repr=False)
    _loaded_path: str | None = field(default=None, init=False, repr=False)

    @property
    def pending_path(self) -> str | None:
        """The path waiting for the machine to go idle, or None."""
        return self._pending_path

    def note_published_file(self, path: str) -> None:
        """Call on an upload-finished or play-started event naming `path`.
        A blank path is ignored (malformed event, nothing to fetch); a path
        already loaded is not re-queued."""
        if path and path != self._loaded_path:
            self._pending_path = path

    def due_fetch(self, is_idle: bool) -> str | None:
        """Call whenever the machine's Idle-ness is (re)observed, e.g. on
        every status update. Returns the path to fetch right now, clearing
        the pending request -- or None if nothing is pending or the machine
        is not idle. The caller should call ``mark_loaded`` once the fetch
        actually completes (or immediately, to avoid re-triggering while a
        slow fetch is still in flight)."""
        if not is_idle or self._pending_path is None:
            return None
        path = self._pending_path
        self._pending_path = None
        return path

    def mark_loaded(self, path: str) -> None:
        """Call once `path` is actually loaded and drawn -- whether that
        fetch was triggered by `due_fetch` or the file was loaded some
        other way (this controller started the job itself, or the operator
        opened it manually). Clears any pending request for the same or an
        older path."""
        self._loaded_path = path
        self._pending_path = None
