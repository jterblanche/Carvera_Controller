"""A real Controller against a fake machine (a real socket): the presence
check.

The machine asks an identified controller "are you there" (0x6D with a
4-byte number) when another hello arrives with its id from a different
launch. The controller answers at once with 0x6E and the same number, from
its network thread, so a busy screen never makes it look gone.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

import carveracontroller.Controller as controller_module
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import (
    PTYPE_PRESENCE_CHECK,
    PTYPE_PRESENCE_REPLY,
    build_frame,
)
from tests.unit.fake_machine import FakeMachine

LAUNCH = 0x6512_3456_0000_00AA
IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC", launch=LAUNCH)


@pytest.fixture
def machines():
    made = []

    def make(**kwargs):
        m = FakeMachine(**kwargs)
        made.append(m)
        return m

    yield make
    for m in made:
        m.stop()


@pytest.fixture
def controller():
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    yield c
    c.close(allow_reconnect=False)


# --- presence check -------------------------------------------------------------


def test_presence_check_is_answered_with_the_same_number(machines, controller):
    m = machines(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.identified, timeout=3.0)

    m.send_frame(build_frame(PTYPE_PRESENCE_CHECK, bytes.fromhex("deadbeef")))

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_PRESENCE_REPLY) != [], timeout=2.0)
    assert m.frames_of_type(PTYPE_PRESENCE_REPLY) == [bytes.fromhex("deadbeef")]


def test_presence_check_is_answered_from_the_network_thread(machines, controller):
    """Not through the screen's clock: the reply is sent by the thread that
    read the check, even while the screen is busy."""
    m = machines(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.identified, timeout=3.0)
    senders = []
    real_send_wire = controller._send_wire

    def recording_send_wire(frame):
        if frame[4] == PTYPE_PRESENCE_REPLY:
            senders.append(threading.current_thread())
        real_send_wire(frame)

    controller._send_wire = recording_send_wire

    with patch.object(controller_module, "Clock") as clock:
        m.send_frame(build_frame(PTYPE_PRESENCE_CHECK, (7).to_bytes(4, "big")))
        assert m.wait_until(lambda: m.frames_of_type(PTYPE_PRESENCE_REPLY) != [], timeout=2.0)
        clock.schedule_once.assert_not_called()

    assert senders == [controller.thread]


def test_every_presence_check_is_answered(machines, controller):
    m = machines(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.identified, timeout=3.0)

    for number in (1, 1, 2):
        m.send_frame(build_frame(PTYPE_PRESENCE_CHECK, number.to_bytes(4, "big")))

    assert m.wait_until(lambda: len(m.frames_of_type(PTYPE_PRESENCE_REPLY)) == 3, timeout=2.0)
    assert m.frames_of_type(PTYPE_PRESENCE_REPLY) == [b"\0\0\0\1", b"\0\0\0\1", b"\0\0\0\2"]


def test_presence_check_trailing_bytes_are_not_echoed(machines, controller):
    m = machines(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.identified, timeout=3.0)

    m.send_frame(build_frame(PTYPE_PRESENCE_CHECK, bytes.fromhex("0102030499")))

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_PRESENCE_REPLY) != [], timeout=2.0)
    assert m.frames_of_type(PTYPE_PRESENCE_REPLY) == [bytes.fromhex("01020304")]


def test_a_presence_check_too_short_to_carry_a_number_is_dropped(machines, controller):
    m = machines(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.identified, timeout=3.0)
    lines = []
    controller.log.put = lambda item: lines.append(item)

    m.send_frame(build_frame(PTYPE_PRESENCE_CHECK, b"\x01\x02"))
    m.send_frame(build_frame(PTYPE_PRESENCE_CHECK, (9).to_bytes(4, "big")))

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_PRESENCE_REPLY) != [], timeout=2.0)
    assert m.frames_of_type(PTYPE_PRESENCE_REPLY) == [(9).to_bytes(4, "big")]
    # Never shown as console text.
    assert not any("\x01\x02" in str(item) for item in lines)
