"""Whether to announce another controller joining or leaving the machine.

The machine publishes a client-joined or client-left event
(``protocols.handshake.ClientPresenceChanged``) to every identified
controller, including the one the event is about. This module decides
whether that event is worth telling the user about; the wording and where it
appears belong to the UI. Kept Kivy-free so it is testable without any UI.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..protocols.handshake import ClientPresenceChanged


@dataclass(frozen=True)
class PresenceAnnouncement:
    """One announcement for the UI: which controller, and which way it went.

    Carries the controller's name only. The machine never sends another
    controller's network address in these events, and nothing here should
    ever show one.
    """

    name: str
    joined: bool


def presence_announcement(event: ClientPresenceChanged, own_id: int, enabled: bool) -> PresenceAnnouncement | None:
    """The announcement to show for `event`, or None when there is nothing
    to show: the user has turned announcements off, or the event is about
    this controller itself (it already knows it just joined)."""
    if not enabled or event.client_id == own_id:
        return None
    return PresenceAnnouncement(name=event.name, joined=event.joined)
