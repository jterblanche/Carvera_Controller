"""Recognising the firmware's control-token refusal reply.

``ControlToken::gate()`` (firmware's ``src/libs/ControlToken.cpp``) refuses a
command outright -- multi-user mode, someone else holds control; or
single-user mode, or control free, with motion already in progress -- by
replying with one line of text, naming the holder where there is one
(``WifiProvider::gate_dispatch()``, ``SerialConsole::gate_dispatch()``):

    error:Refused -- <holder> has control
    error:Transfer refused -- <holder> has control and an interactive move is in progress
    error:Transfer refused -- an interactive move is in progress

Both links send exactly this text -- WiFi wraps it in a ``PTYPE_NORMAL_INFO``
frame, USB sends it as a plain line -- and ``Controller.parseLine``'s own
"error"/"alarm" branch already shows either verbatim, the same way a refused
"suspend" is shown (see ``tests/unit/test_passive_state.py``). This module
exists so a listing or an upload in progress -- which route incoming text
somewhere other than ``parseLine`` while they wait for their own reply -- can
recognise this one specific reply among their own data and stop waiting for
it, instead of either queuing it as data (a directory listing) or ignoring it
outright (``XMODEM.send()``, which otherwise discards every reply type below
its own file-transfer range -- see ``PTYPE_FILE_MD5`` in
``protocols/framing.py``).
"""

from __future__ import annotations

import re

_REFUSAL = re.compile(r"error:\s*(refused|transfer refused)\b", re.IGNORECASE)


def is_control_refusal(text: str) -> bool:
    """True for the control gate's own refusal text (above), and nothing
    else that merely contains the word "error" -- an alarm, or a different
    error reply, must not be mistaken for this one specific reply."""
    return bool(text) and _REFUSAL.match(text.strip()) is not None
