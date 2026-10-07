"""A real Controller against a fake machine (a real socket): the presence
check, and hello-ack results 3 and 4.

- The machine asks an identified controller "are you there" (0x6D with a
  4-byte number) when another hello arrives with its id from a different
  launch. The controller answers at once with 0x6E and the same number, from
  its network thread, so a busy screen never makes it look gone.
- Result 3: refused as a duplicate. Close, say why, do not retry.
- Result 4: busy. Send the same hello again about a second later, silently.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import carveracontroller.Controller as controller_module
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine import hello as hello_module
from carveracontroller.machine.hello import ACK_TIMEOUT_S, Resolution
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import (
    PTYPE_AUTO_COMMAND,
    PTYPE_CTRL_MULTI,
    PTYPE_HELLO,
    PTYPE_HELLO_ACK,
    PTYPE_PRESENCE_CHECK,
    PTYPE_PRESENCE_REPLY,
    build_frame,
)
from carveracontroller.protocols.handshake import (
    HELLO_ACCEPTED,
    HELLO_BUSY,
    HELLO_FEATURE_JOB_START_WAIT,
    HELLO_REJECTED_IDENTITY_CONNECTED,
)
from tests.unit.fake_machine import FakeMachine

LAUNCH = 0x6512_3456_0000_00AA
IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC", launch=LAUNCH)


def scripted_acks(results, delay=0.0):
    """A frame hook answering each hello with the next result in ``results``
    (the last one repeats)."""
    remaining = list(results)

    def hook(conn, ptype, payload):
        if ptype != PTYPE_HELLO:
            return False
        result = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if delay:
            time.sleep(delay)
        conn.sendall(build_frame(PTYPE_HELLO_ACK, bytes([1, result, 0, 0])))
        return True

    return hook


@pytest.fixture
def fake_app():
    app = MagicMock()
    with (
        patch.object(controller_module, "App") as mock_app_cls,
        patch.object(controller_module, "Clock") as mock_clock,
    ):
        mock_app_cls.get_running_app.return_value = app
        app.clock = mock_clock
        yield app


def run_scheduled(app):
    for call in app.clock.schedule_once.call_args_list:
        call.args[0](0)


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


# --- result 3 -------------------------------------------------------------------


def test_result_3_closes_shows_the_duplicate_message_and_does_not_retry(machines, fake_app):
    m = machines(mode="new", frame_hook=scripted_acks([HELLO_REJECTED_IDENTITY_CONNECTED], delay=1.5))
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    c.reconnect_enabled = True
    c.reconnect_callback = MagicMock()
    try:
        c.open(CONN_WIFI, m.address())
        c.executeCommand("version")  # held back; must never be sent

        assert m.wait_until(lambda: c.stream is None, timeout=5.0)
        run_scheduled(fake_app)

        fake_app.root.show_hello_rejected_popup.assert_called_once_with("duplicate")
        assert m.frames_of_type(PTYPE_CTRL_MULTI) == []
        assert m.frames_of_type(PTYPE_AUTO_COMMAND) == []
        assert c._manual_disconnect is True
        c.reconnect_callback.assert_not_called()
        time.sleep(1.5)
        assert len(m.frames_of_type(PTYPE_HELLO)) == 1
    finally:
        c.close(allow_reconnect=False)


# --- result 4 -------------------------------------------------------------------


def test_result_4_resends_the_same_hello_silently_until_accepted(machines, fake_app):
    m = machines(mode="new", frame_hook=scripted_acks([HELLO_BUSY, HELLO_BUSY, HELLO_ACCEPTED]))
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    try:
        c.open(CONN_WIFI, m.address())
        c.executeCommand("version")

        assert m.wait_until(lambda: c._hello is not None and c._hello.identified, timeout=6.0)

        hellos = m.frames_of_type(PTYPE_HELLO)
        assert len(hellos) == 3
        assert hellos[0] == hellos[1] == hellos[2]
        assert c.stream is not None
        run_scheduled(fake_app)
        fake_app.root.show_hello_rejected_popup.assert_not_called()
        # The held command goes out once identified.
        assert m.wait_until(lambda: m.frames_of_type(PTYPE_CTRL_MULTI) == [b"version"], timeout=2.0)
    finally:
        c.close(allow_reconnect=False)


def test_result_4_retries_are_about_a_second_apart(machines):
    times = []
    hook = scripted_acks([HELLO_BUSY, HELLO_BUSY, HELLO_ACCEPTED])

    def timing_hook(conn, ptype, payload):
        if ptype == PTYPE_HELLO:
            times.append(time.monotonic())
        return hook(conn, ptype, payload)

    m = machines(mode="new", frame_hook=timing_hook)
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    try:
        c.open(CONN_WIFI, m.address())
        assert m.wait_until(lambda: len(times) >= 3, timeout=6.0)

        gaps = [b - a for a, b in zip(times, times[1:])]
        assert all(0.9 <= gap <= 1.5 for gap in gaps), gaps
    finally:
        c.close(allow_reconnect=False)


def test_result_4_gives_up_with_machine_busy(machines, fake_app, monkeypatch):
    monkeypatch.setattr(hello_module, "BUSY_RETRY_S", 0.2)
    monkeypatch.setattr(hello_module, "BUSY_GIVE_UP_S", 1.5)
    m = machines(mode="new", frame_hook=scripted_acks([HELLO_BUSY]))
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    try:
        c.open(CONN_WIFI, m.address())

        assert m.wait_until(lambda: c.stream is None, timeout=5.0)
        run_scheduled(fake_app)

        fake_app.root.show_hello_rejected_popup.assert_called_once_with("busy")
        assert c._manual_disconnect is True
        assert len(m.frames_of_type(PTYPE_HELLO)) >= 3
    finally:
        c.close(allow_reconnect=False)


# --- old firmware ---------------------------------------------------------------


def test_old_firmware_still_works_with_the_launch_part_in_the_hello(machines, controller):
    """Firmware that never answers hello: it receives the longer hello,
    ignores it, and every queued command still goes out, unwrapped, as
    before."""
    m = machines(mode="old")
    controller.open(CONN_WIFI, m.address())
    controller.executeCommand("version")

    assert m.wait_until(lambda: len(m.hellos_received) >= 1, timeout=3.0)
    payload = m.hellos_received[0]
    assert payload[-9] == HELLO_FEATURE_JOB_START_WAIT
    assert payload[-8:] == LAUNCH.to_bytes(8, "big")

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_CTRL_MULTI) == [b"version"], timeout=ACK_TIMEOUT_S + 2.0)
    assert controller._hello.resolution is Resolution.FALLBACK
    assert m.frames_of_type(PTYPE_PRESENCE_REPLY) == []


def test_firmware_that_acks_without_checking_identifies_at_once(machines, controller):
    """Current fork firmware: ignores the launch part and accepts at once."""
    m = machines(mode="new", ack_result=HELLO_ACCEPTED)
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: controller._hello is not None and controller._hello.identified, timeout=2.0)
    assert len(m.frames_of_type(PTYPE_HELLO)) == 1
