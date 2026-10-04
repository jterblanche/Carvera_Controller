"""Whether the machine's reported state means a settings write would be
refused.

Status reports carry the machine's state as their leading word
(``Controller.parseBracketAngle``: ``CNC.vars["state"] = l[0]``), mirrored
onto the running app as ``MakeraApp.state`` (see ``main.py``'s
``update_jog_controls_enabled`` for another consumer of that same
property). Firmware's config-write gate (``ConfigWriteGate``, fork PR #33)
refuses ``config-set``/``-delete``/``-load``/``-restore``/``-default``
whenever ``Kernel::get_state()`` is anything other than Idle, Alarm or
Sleep -- Idle and Sleep because nothing is moving, Alarm too so a setting
that caused the alarm can still be corrected. This module mirrors that
same rule on the controller side, so the settings page can hold a write
back before sending it rather than relying solely on the firmware's own
refusal (which still applies against firmware without that gate -- see
``refuse_machine_settings_write`` in ``main.py``).
"""

from __future__ import annotations

# The three states a write is allowed in. Every other value reported by a
# status report -- Run, Hold, Home, Wait, Tool, Pause among them -- counts
# as busy. See STATECOLOR in Controller.py for the full vocabulary of
# state names the app recognises (it also carries "Disable", which is a
# blink-off display colour, never an actual reported state).
IDLE_LIKE_STATES = frozenset({"Idle", "Alarm", "Sleep"})


def machine_is_busy(state: str) -> bool:
    """True for every machine state except Idle, Alarm and Sleep.

    This also covers the not-connected sentinel ("N/A", ``main.py``'s
    ``NOT_CONNECTED``) and anything unrecognised: with no confirmed Idle,
    Alarm or Sleep report to allow a write, treating the state as busy
    fails safe -- the same outcome a stale or missing status would need
    anyway, rather than optimistically allowing a write that currently has
    nowhere confirmed to land.
    """
    return state not in IDLE_LIKE_STATES
