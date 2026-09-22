"""Automatic heartbeat scheduling for a subscribed (identified) connection.

Pure timing decision, no I/O — mirrors the firmware's own `publish_due()`
(`libs/Publish.cpp`): "is it time to send again," given now and when
something was last sent. The heartbeat (`0x62`) is empty, automatic traffic
a subscribed controller sends whenever nothing else has gone out on the
link recently, so the machine — and the WiFi module's own idle timer, which
counts traffic in either direction — keeps seeing this link as live even
when the controller has nothing else to say.
"""

from __future__ import annotations

# 3 s, chosen to sit comfortably inside the 10 s wifi.tcp_timeout_s default
# (measured drop ~11.5 s), so a fully idle, subscribed controller is never
# mistaken for a dead one.
HEARTBEAT_INTERVAL_S = 3.0


def heartbeat_due(now: float, last_sent_at: float | None, interval_s: float = HEARTBEAT_INTERVAL_S) -> bool:
    """True if a heartbeat is due: nothing has gone out on this link for at
    least ``interval_s``, or nothing has gone out at all yet
    (``last_sent_at`` is ``None``).

    The caller passes the timestamp of the *last send of any kind* on this
    link, not a heartbeat-specific one — sending anything else (a command,
    a query, realtime bytes, a previous heartbeat) resets the same clock,
    since the point is to keep the link looking like live traffic, not to
    send a heartbeat on a fixed schedule regardless of what else went out.
    """
    return last_sent_at is None or (now - last_sent_at) >= interval_s
