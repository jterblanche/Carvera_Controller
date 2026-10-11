"""The other-connected-controllers model, built from the client-list reply.

Pure presentation logic: turns the decoded client-list entries
(``protocols.handshake.ClientEntry``) into ready-to-display rows. Kept
Kivy-free and framework-agnostic so it is testable without any UI.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..protocols.handshake import LINK_USB, LINK_WIFI, ClientEntry


@dataclass(frozen=True)
class ClientRow:
    """One row for a connected-controllers display. ``link`` says how the
    controller is connected: "USB", "WiFi", "WiFi - <IP address>" when
    another WiFi controller shows the same name, or "" when not known."""

    name: str
    has_control: bool
    is_self: bool
    link: str = ""


def link_label(entry: ClientEntry, entries: tuple[ClientEntry, ...]) -> str:
    """How `entry` is connected, for its row: "USB" or "WiFi", and for a
    WiFi entry whose name shows the same as another WiFi entry's, its IP
    address too ("WiFi - 192.168.1.23"), when the machine sent one. ""
    for a link value this controller does not know."""
    if entry.link == LINK_USB:
        return "USB"
    if entry.link != LINK_WIFI:
        return ""
    shown = truncate_name(entry.name)
    clash = any(
        other is not entry and other.link == LINK_WIFI and truncate_name(other.name) == shown for other in entries
    )
    if clash and entry.ip:
        return f"WiFi - {entry.ip}"
    return "WiFi"


def rows_for_display(entries: tuple[ClientEntry, ...], own_id: int) -> tuple[ClientRow, ...]:
    """Build display rows from a decoded client list, controlling entry
    first, then by name, then by link."""
    rows = (
        ClientRow(
            name=entry.name,
            has_control=entry.has_control,
            is_self=entry.id == own_id,
            link=link_label(entry, entries),
        )
        for entry in entries
    )
    return tuple(sorted(rows, key=lambda row: (not row.has_control, row.name.lower(), row.link)))


# A name this long, plus a status suffix, still fits one line of the status
# drop-down's connected-controllers list without wrapping. A wrapped line
# reads as a second controller, so a name over the limit is
# ellipsised instead of left to wrap. Wire names can be up to 31 bytes
# (protocols.handshake._MAX_NAME_BYTES); this is shorter than that on
# purpose, to leave room for the link, " (you)" and " — Has Control".
MAX_DISPLAY_NAME_LEN = 20


def truncate_name(name: str, max_len: int = MAX_DISPLAY_NAME_LEN) -> str:
    """Shorten `name` to `max_len` characters, ellipsis instead of wrapping.
    Names at or under the limit are returned unchanged."""
    if len(name) <= max_len:
        return name
    return name[: max_len - 1] + "…"


def row_display_text(
    row: ClientRow,
    *,
    you_suffix: str,
    control_suffix: str,
    max_name_len: int = MAX_DISPLAY_NAME_LEN,
) -> str:
    """The single-line label for one connected-controllers row: the
    (possibly truncated) name, its link in brackets when known, then who
    is you and who has control. Kept translation-agnostic -- callers pass
    already-translated suffixes -- so this stays Kivy- and locale-free and
    testable on its own."""
    text = truncate_name(row.name, max_name_len)
    if row.link:
        text += f" ({row.link})"
    if row.is_self:
        text += you_suffix
    if row.has_control:
        text += control_suffix
    return text


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


def entries_with_holder(entries: tuple[ClientEntry, ...], holder_id: int) -> tuple[ClientEntry, ...]:
    """The same client list with ``has_control`` true on the entry whose id
    is ``holder_id`` and false on every other one. ``holder_id`` 0 (the
    machine's "nobody holds control") or an id not in the list leaves no
    entry marked. Used when a control-changed event arrives: the machine
    sends no new client list then, so the marks from the last one would
    otherwise keep naming the previous holder."""
    return tuple(replace(entry, has_control=holder_id != 0 and entry.id == holder_id) for entry in entries)
