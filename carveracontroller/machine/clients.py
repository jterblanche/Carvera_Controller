"""The other-connected-controllers model, built from the client-list reply.

Pure presentation logic: turns the decoded client-list entries
(``protocols.handshake.ClientEntry``) into ready-to-display rows. Kept
Kivy-free and framework-agnostic so it is testable without any UI.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..protocols.handshake import ClientEntry


@dataclass(frozen=True)
class ClientRow:
    """One row for a connected-controllers display."""

    name: str
    has_control: bool
    is_self: bool


def rows_for_display(entries: tuple[ClientEntry, ...], own_id: int) -> tuple[ClientRow, ...]:
    """Build display rows from a decoded client list, controlling entry first."""
    ordered = sorted(entries, key=lambda entry: (not entry.has_control, entry.name.lower()))
    return tuple(
        ClientRow(name=entry.name, has_control=entry.has_control, is_self=entry.id == own_id) for entry in ordered
    )


def holder_from_client_list(entries: tuple[ClientEntry, ...]) -> tuple[int, str]:
    """Who the client list says holds control right now: the id and name of
    the one entry whose ``has_control`` is true, or ``(0, "")`` -- the
    machine's own "nobody holds control" encoding -- when no entry has it
    true, including an empty list and old firmware's client-list reply,
    which never sets the field at all."""
    for entry in entries:
        if entry.has_control:
            return entry.id, entry.name
    return 0, ""
