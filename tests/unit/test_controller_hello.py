"""End-to-end identify-handshake tests: a real Controller against a real
socket (FakeMachine), covering ticket #18's acceptance criteria that need
more than a single protocol/state-machine unit can show on its own.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import carveracontroller.Controller as controller_module
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import (
    PTYPE_AUTO_COMMAND,
    PTYPE_CTRL_MULTI,
    PTYPE_CTRL_SINGLE,
    PTYPE_HELLO,
)
from carveracontroller.protocols.handshake import HELLO_REJECTED_CAP
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")


@pytest.fixture
def machine():
    instances = []

    def _make(**kwargs):
        m = FakeMachine(**kwargs)
        instances.append(m)
        return m

    yield _make
    for m in instances:
        m.stop()


@pytest.fixture
def controller():
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    yield c
    c.close(allow_reconnect=False)


def test_hello_not_sent_to_a_machine_that_never_replies(machine, controller):
    """No CRC-valid frame ever arrives from this machine, so hello must
    never be sent (protocol doc §3)."""
    m = machine(mode="silent")
    controller.open(CONN_WIFI, m.address())

    time.sleep(0.3)

    assert m.hellos_received == []
    assert controller.comms.frame_confirmed is False


def test_hello_sent_once_a_valid_frame_is_confirmed(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: len(m.hellos_received) >= 1, timeout=2.0)
    assert controller.comms.frame_confirmed is True

    payload = m.hellos_received[0]
    assert payload[0] == 1  # protocol_version
    assert int.from_bytes(payload[1:9], "big") == IDENTITY.id
    name_len = payload[9]
    assert payload[10 : 10 + name_len].decode() == IDENTITY.name
    assert payload[10 + name_len] == 0  # link = WiFi


def test_hello_sent_exactly_once(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: len(m.hellos_received) >= 1)

    time.sleep(0.5)
    assert len(m.hellos_received) == 1


def test_nothing_but_realtime_and_hello_before_ack_then_unwrapped_fallback(machine, controller):
    """Old firmware never acks. Before the 1.0s fallback window elapses,
    the machine must see nothing but realtime (0xA1) and hello (0x60)
    frames; the attempted command must then arrive unwrapped, exactly as
    today, once fallback resolves."""
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())
    controller.executeCommand("version")

    assert m.wait_until(lambda: len(m.hellos_received) >= 1)

    time.sleep(0.5)  # well inside the 1.0s ack window
    seen_types = {t for t, _ in m.frames_received}
    assert seen_types <= {PTYPE_CTRL_SINGLE, PTYPE_HELLO}
    assert m.frames_of_type(PTYPE_CTRL_MULTI) == []

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_CTRL_MULTI) != [], timeout=2.0)
    (sent,) = m.frames_of_type(PTYPE_CTRL_MULTI)
    assert sent == b"version"


def test_connect_time_query_wrapped_after_accepted_ack(machine, controller):
    m = machine(mode="new", ack_delay=0.3)
    controller.open(CONN_WIFI, m.address())
    controller.executeCommand("version")

    time.sleep(0.15)  # before the ack arrives
    assert m.frames_of_type(PTYPE_CTRL_MULTI) == []
    assert m.frames_of_type(PTYPE_AUTO_COMMAND) == []

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_AUTO_COMMAND) != [], timeout=2.0)
    assert m.frames_of_type(PTYPE_CTRL_MULTI) == []  # never sent unwrapped
    (wrapped,) = m.frames_of_type(PTYPE_AUTO_COMMAND)
    assert wrapped[0] == 0  # kind = console command
    assert wrapped[1:] == b"version"


def test_client_list_requested_and_stored_after_identify(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: m.client_list_requests >= 1, timeout=2.0)
    assert controller.identity is IDENTITY
    assert m.wait_until(lambda: len(controller.connected_clients) == 1, timeout=2.0)
    entry = controller.connected_clients[0]
    assert entry.name == "Fake Machine Self"
    assert entry.has_control is True


def test_ack_rejected_cap_does_not_identify(machine, controller):
    m = machine(mode="new", ack_result=HELLO_REJECTED_CAP)
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: len(m.hellos_received) >= 1)

    time.sleep(0.3)
    assert controller._hello is not None
    assert controller._hello.identified is False
    assert m.client_list_requests == 0


def test_accepted_then_closed_before_identify_shows_busy_not_lost():
    """Old firmware with another client attached: accepts the TCP connection
    then closes it at once, before any protocol exchange. The controller
    must recognise this as "machine busy" (today's message), not fall
    through to the heartbeat-timeout "connection lost" flow, and must not
    start an auto-reconnect loop."""
    m = FakeMachine(mode="old", close_after=0.06)
    controller = Controller(CNC(), callback=None, identity=IDENTITY)
    fake_app = MagicMock()
    try:
        with (
            patch.object(controller_module, "App") as mock_app_cls,
            patch.object(controller_module, "Clock") as mock_clock,
        ):
            mock_app_cls.get_running_app.return_value = fake_app
            controller.open(CONN_WIFI, m.address())

            deadline_ok = m.wait_until(lambda: controller.stream is None, timeout=2.0)
            assert deadline_ok, "controller must give up the link once the machine closes it"
            assert controller.comms.frame_confirmed is False

            assert mock_clock.schedule_once.called
            scheduled = [call.args[0] for call in mock_clock.schedule_once.call_args_list]
            for fn in scheduled:
                fn(0)
            fake_app.root.show_machine_busy_before_identify_popup.assert_called_once()
    finally:
        controller.close(allow_reconnect=False)
        m.stop()
