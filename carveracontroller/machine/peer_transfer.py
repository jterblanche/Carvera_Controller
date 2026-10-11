"""Another controller's file transfer, as this controller sees it.

While the machine runs a WiFi file transfer for one controller, it reads
every other controller's bytes but cannot act on them. It discards them and
answers with one line of text, sent to that controller alone:

    error:Busy -- a file transfer is in progress, retry when it finishes

It answers the first read from each controller during the transfer, then
only reads that hold something other than automatic traffic, and at most
every 250 ms. Status queries and heartbeats get nothing more after the first
answer, and the machine publishes no status at all until the transfer ends
(firmware ``libs/TransferBusy.h``, ``WifiProvider::refuse_as_busy()`` and
``WifiProvider::on_idle()``). So a controller that only listens for status
sees a silent machine and would call the link lost, though the machine is
fine and the transfer can run for minutes.

``PeerTransferWatch`` keeps track of one such transfer. While it runs, the
controller asks for the client list (``0x63``) once every
``PROBE_INTERVAL_S``: a frame the machine answers with the busy reply during
the transfer, and with the client list once it has ended, which it acts on
like any other. Every answer proves the link is still there, and silence
still means it is not. The first frame that is not a busy reply means the
transfer has ended.

A transfer over the other link is worse: the machine reads nothing from
WiFi while it runs a USB transfer, and nothing from USB while it runs a
WiFi one (both ``on_idle()`` handlers return at once while a transfer
runs), so the other controllers get no busy reply and no frame at all. Only
the controller starting the transfer can tell them, so it relays a notice
first (``protocols/relay.py``) giving how long the transfer may take, and
another when it ends. Until then, or until that time has passed, the
others treat the silence as the transfer, not a lost link. Pure state, no
I/O and no clock of its own.
"""

from __future__ import annotations

from .heartbeat import heartbeat_due

# The machine's reply, without the advice that follows it. Matched at the
# start of the line, ignoring case and surrounding whitespace.
BUSY_REPLY_PREFIX = "error:Busy -- a file transfer is in progress"

# How often to ask while another controller's transfer runs. Each ask gets
# an answer (the machine's own limit is one per 250 ms), so the link is
# proven every second, well inside the 5 s the app allows before it calls
# the link lost (main.py's HEARTBEAT_TIMEOUT).
PROBE_INTERVAL_S = 1.0

# How long a controller tells the others its transfer may take: a fixed
# margin plus one second per this many bytes. 1000 B/s is a deliberately
# low floor (USB at 115200 baud carries at most about 11 KB/s before the
# transfer protocol's own overhead), because a transfer that outlasts its
# notice is cut off as a lost link by the others. Ending sooner costs
# nothing: the end notice clears it.
ALLOWANCE_MARGIN_S = 30
ALLOWANCE_BYTES_PER_S = 1000
# When the size is not known before the transfer starts (a download).
DEFAULT_ALLOWANCE_S = 600


def transfer_allowance_s(size_bytes: int | None) -> int:
    """Seconds to announce for a transfer of ``size_bytes`` on the wire, or
    DEFAULT_ALLOWANCE_S when that is not known."""
    if size_bytes is None or size_bytes < 0:
        return DEFAULT_ALLOWANCE_S
    return min(ALLOWANCE_MARGIN_S + size_bytes // ALLOWANCE_BYTES_PER_S, 0xFFFF)


def is_transfer_busy_reply(text: str) -> bool:
    """True for the machine's busy reply (above) and nothing else."""
    return bool(text) and text.strip().lower().startswith(BUSY_REPLY_PREFIX.lower())


class PeerTransferWatch:
    """Whether the machine is busy with another controller's transfer, when
    to ask again while it is, and which busy replies are worth showing."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Back to no transfer known, as on a new connection."""
        self.active = False
        self._last_probe_at: float | None = None
        self._command_sent = False
        self._announced_until: float | None = None

    def running(self, now: float) -> bool:
        """True while the machine is known to be busy with another
        controller's transfer: it has sent the busy reply, or the other
        controller's notice has not yet ended or run out."""
        return self.active or self.announced(now)

    def announce(self, now: float, allowance_s: float) -> None:
        """Another controller relayed that its transfer starts now and may
        take up to ``allowance_s`` seconds."""
        self._announced_until = now + allowance_s

    def announced(self, now: float) -> bool:
        return self._announced_until is not None and now < self._announced_until

    def end_announcement(self) -> None:
        """Its end notice arrived."""
        self._announced_until = None

    def expire(self, now: float) -> bool:
        """Forget a notice whose time has run out. Returns True if it did."""
        if self._announced_until is None or now < self._announced_until:
            return False
        self._announced_until = None
        return True

    def on_busy_reply(self) -> bool:
        """A busy reply arrived. Returns True when it should be shown on the
        console: the first one of a transfer, and the first one after this
        controller sent a command of its own (note_command_sent), which the
        machine has just turned away. The rest answer the probes."""
        show = not self.active or self._command_sent
        if not self.active:
            self.active = True
            self._last_probe_at = None
        self._command_sent = False
        return show

    def on_other_frame(self) -> bool:
        """Any other frame arrived. Returns True when this ends a transfer
        that sent the busy reply: the machine sends nothing else to this
        controller until it has. Other frames can still arrive between a
        notice and the transfer it announces, so they leave a notice alone
        unless a busy reply has come since."""
        if not self.active:
            return False
        self.reset()
        return True

    def note_command_sent(self) -> None:
        """This controller sent a command of its own during the transfer."""
        if self.active:
            self._command_sent = True

    def probe_due(self, now: float) -> bool:
        """True while a transfer runs and PROBE_INTERVAL_S has passed since
        the last probe, or none has gone yet."""
        return self.active and heartbeat_due(now, self._last_probe_at, PROBE_INTERVAL_S)

    def probe_sent(self, now: float) -> None:
        self._last_probe_at = now
