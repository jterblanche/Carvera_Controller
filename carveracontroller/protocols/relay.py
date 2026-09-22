"""What this controller puts inside a relay (`0x67`) frame's payload.

The machine treats a relay payload as opaque bytes -- it only repeats it to
every other identified client, prefixed with the sender's id. What those
bytes mean is a convention between controllers, not part of the wire
protocol the machine itself understands. This file defines the one
convention this controller uses today: a compact summary of the currently
loaded tool table, so a controller that does not have the G-code file open
can still show a sensible tool name at a tool-change prompt.

A relay's own payload is capped by the machine at 527 bytes (535 minus the
8-byte source id it prepends before repeating the frame) -- see
``max_entry_text_bytes`` below for how that is divided.
"""

from __future__ import annotations

# The only relay payload kind this controller sends or understands today.
# The first byte of every payload this module builds, so a decoder can tell
# a tool-table summary apart from any other relay traffic (its own, or a
# future kind) without guessing from the shape of the bytes.
RELAY_KIND_TOOL_TABLE = 1

# 527 B relay payload cap, minus kind(1) + count(1).
_HEADER_BYTES = 2
# Per entry: tool_number(2) + text_len(1) + text.
_ENTRY_HEADER_BYTES = 3
# Leaves room for a handful of entries with a reasonably descriptive label
# each, well inside the machine's ATC tool count in practice.
max_entry_text_bytes = 60
_MAX_RELAY_PAYLOAD_BYTES = 527


def encode_tool_table_relay(entries: dict[int, str]) -> bytes:
    """Build a relay payload summarising a tool table: kind(1) + count(1) +
    entries of tool_number(2, BE) + text_len(1) + text (UTF-8, truncated to
    ``max_entry_text_bytes``).

    Entries are added in ``entries`` iteration order until the next one
    would not fit in the 527 B relay payload cap, then stop -- the same
    "build what fits, drop the rest" style the firmware's own wire builders
    use, rather than raising. An empty table encodes as kind + count(0).
    """
    out = bytearray([RELAY_KIND_TOOL_TABLE, 0])
    count = 0
    for number, text in entries.items():
        raw = text.encode("utf-8")[:max_entry_text_bytes]
        # A multi-byte UTF-8 character straddling the truncation point
        # would produce invalid UTF-8; back off until it decodes cleanly.
        while raw:
            try:
                raw.decode("utf-8")
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
        entry_len = _ENTRY_HEADER_BYTES + len(raw)
        if len(out) + entry_len > _MAX_RELAY_PAYLOAD_BYTES:
            break
        out += int(number & 0xFFFF).to_bytes(2, "big")
        out.append(len(raw))
        out += raw
        count += 1
    out[1] = count
    return bytes(out)


def decode_tool_table_relay(payload: bytes) -> dict[int, str] | None:
    """Decode a relay payload built by ``encode_tool_table_relay``.

    Returns None if the first byte is not ``RELAY_KIND_TOOL_TABLE`` (some
    other relay traffic this controller does not understand -- dropped
    silently by the caller, the same tolerant style every other decoder in
    this package uses) or the payload is too short to hold its own count.
    A malformed entry stops decoding and returns whatever complete entries
    were parsed so far, rather than raising.
    """
    if len(payload) < _HEADER_BYTES or payload[0] != RELAY_KIND_TOOL_TABLE:
        return None
    count = payload[1]
    entries: dict[int, str] = {}
    offset = _HEADER_BYTES
    for _ in range(count):
        if offset + _ENTRY_HEADER_BYTES > len(payload):
            break
        number = int.from_bytes(payload[offset : offset + 2], "big")
        offset += 2
        text_len = payload[offset]
        offset += 1
        if offset + text_len > len(payload):
            break
        text = payload[offset : offset + text_len].decode("utf-8", errors="replace")
        offset += text_len
        entries[number] = text
    return entries
