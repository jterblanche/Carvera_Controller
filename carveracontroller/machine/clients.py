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


# A name this long, plus a status suffix, still fits one line of the status
# drop-down's connected-controllers list without wrapping. A wrapped line
# reads as a second controller, so a name over the limit is
# ellipsised instead of left to wrap. Wire names can be up to 31 bytes
# (protocols.handshake._MAX_NAME_BYTES); this is shorter than that on
# purpose, to leave room for " (you)" / " — in control".
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
    (possibly truncated) name, then who is you and who has control. Kept
    translation-agnostic -- callers pass already-translated suffixes -- so
    this stays Kivy- and locale-free and testable on its own."""
    text = truncate_name(row.name, max_name_len)
    if row.is_self:
        text += you_suffix
    if row.has_control:
        text += control_suffix
    return text
