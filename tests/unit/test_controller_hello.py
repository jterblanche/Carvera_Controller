"""End-to-end identify-handshake tests: a real Controller against a real
socket (FakeMachine), covering the identify-handshake behaviour that needs
more than a single protocol/state-machine unit can show on its own.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import carveracontroller.Controller as controller_module
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_USB, CONN_WIFI, Controller
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
    never be sent."""
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


def test_re_hello_continues_then_stays_bounded_against_old_firmware(machine, controller):
    """Regression for a real bug: re-hello and the ack-timeout timer used
    to share one timestamp, so a steady stream of status replies (exactly
    what old firmware still sends for ordinary polling) could keep
    deferring the 1.0s fallback indefinitely. Running well past that point
    proves re-hello keeps happening (the controller doesn't just give up
    after falling back) but stays bounded (roughly once per second, not on
    every status reply) against firmware that never acks at all."""
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: len(m.hellos_received) >= 1, timeout=1.0)

    time.sleep(2.2)

    count = len(m.hellos_received)
    assert count >= 2, "re-hello must still happen past the 1.0s fallback, within the machine's hello window"
    assert count <= 5, "re-hello must stay roughly once per second, not fire on every status reply"


def test_automatic_query_wrapped_even_when_called_well_after_identification(machine, controller):
    """Regression for the original bug: only a send that happened to still
    be queued when the ack arrived got wrapped, so an allow-listed query
    fired after the ack had already arrived (the common case — acks
    measure about 70ms, well under where these queries are actually
    scheduled from) went out unwrapped, unintentionally taking control.
    Wrapping is now decided by whether the controller is identified, not
    by whether this particular call happened to race the ack."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: m.client_list_requests >= 1, timeout=2.0)  # definitely identified by now

    controller.queryModel()

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_AUTO_COMMAND) != [], timeout=2.0)
    assert m.frames_of_type(PTYPE_CTRL_MULTI) == []
    (wrapped,) = m.frames_of_type(PTYPE_AUTO_COMMAND)
    assert wrapped[0] == 0  # kind = console command
    assert wrapped[1:] == b"model"


def test_automatic_query_wrapped_once_the_delayed_ack_arrives(machine, controller):
    m = machine(mode="new", ack_delay=0.3)
    controller.open(CONN_WIFI, m.address())
    controller.queryVersion()

    time.sleep(0.15)  # before the ack arrives
    assert m.frames_of_type(PTYPE_CTRL_MULTI) == []
    assert m.frames_of_type(PTYPE_AUTO_COMMAND) == []

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_AUTO_COMMAND) != [], timeout=2.0)
    assert m.frames_of_type(PTYPE_CTRL_MULTI) == []  # never sent unwrapped
    (wrapped,) = m.frames_of_type(PTYPE_AUTO_COMMAND)
    assert wrapped[0] == 0  # kind = console command
    assert wrapped[1:] == b"version"


def test_ordinary_command_never_wrapped_even_when_identified(machine, controller):
    """A genuine user/UI-caused command must always go out on the ordinary
    channel, unwrapped — even once identified. Wrapping it would just get
    it refused (it isn't on the machine's automatic allow-list unless it
    happens to be one of the specific queries that is, and even then it
    would be wrong to treat a real user action as automatic)."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: m.client_list_requests >= 1, timeout=2.0)  # fully identified by now

    controller.executeCommand("version")

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_CTRL_MULTI) != [], timeout=2.0)
    assert m.frames_of_type(PTYPE_AUTO_COMMAND) == []


def test_client_list_requested_and_stored_after_identify(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: m.client_list_requests >= 1, timeout=2.0)
    assert controller.identity is IDENTITY
    assert m.wait_until(lambda: len(controller.connected_clients) == 1, timeout=2.0)
    entry = controller.connected_clients[0]
    assert entry.name == "Fake Machine Self"
    assert entry.has_control is True


def test_smoothie_link_never_sends_hello_and_never_gates_sends(machine, controller):
    """A machine still speaking the legacy plaintext protocol: the
    protocol detector picks Smoothie mode, hello is never applicable there
    at all (never sent), and ordinary commands are never held back either
    — end to end, not just at the negotiator-unit level."""
    m = machine(mode="smoothie")
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: b"echo" in bytes(m.smoothie_bytes_received), timeout=2.0)
    assert controller.comms.name == "smoothie"

    controller.executeCommand("version")

    assert m.wait_until(lambda: b"version" in bytes(m.smoothie_bytes_received), timeout=2.0)
    # No framed header ever appears anywhere in the byte stream: hello (or
    # any framed message) was never attempted on this link.
    assert bytes([0x86, 0x68]) not in bytes(m.smoothie_bytes_received)


def test_ack_rejected_closes_the_link_shows_message_and_drops_held_sends():
    """A rejected ack (cap reached, or an old controller present) means the
    machine is about to close this link on its own. The controller closes
    it itself first — no lingering reconnect loop against a machine that
    just refused it — shows the rejection message, and drops anything that
    was held back waiting for the handshake to resolve rather than send it
    into a connection that's being torn down."""
    m = FakeMachine(mode="new", ack_result=HELLO_REJECTED_CAP, ack_delay=0.2)
    controller = Controller(CNC(), callback=None, identity=IDENTITY)
    fake_app = MagicMock()
    try:
        with (
            patch.object(controller_module, "App") as mock_app_cls,
            patch.object(controller_module, "Clock") as mock_clock,
        ):
            mock_app_cls.get_running_app.return_value = fake_app
            controller.open(CONN_WIFI, m.address())
            streamio_thread = controller.thread
            controller.executeCommand("version")  # held back; must never be sent

            assert m.wait_until(lambda: len(m.hellos_received) >= 1, timeout=2.0)
            assert m.wait_until(lambda: controller.stream is None, timeout=2.0)

            assert m.frames_of_type(PTYPE_CTRL_MULTI) == []
            assert m.frames_of_type(PTYPE_AUTO_COMMAND) == []

            # _close_inline must stop streamIO itself (stopRun()), not rely
            # on some other test's close() happening to clear the shared
            # stop Event first — join it directly, on its own, here.
            streamio_thread.join(timeout=2.0)
            assert not streamio_thread.is_alive()
            assert controller._manual_disconnect is True

            scheduled = [call.args[0] for call in mock_clock.schedule_once.call_args_list]
            for fn in scheduled:
                fn(0)
            fake_app.root.show_hello_rejected_popup.assert_called_once_with("cap")
    finally:
        controller.close(allow_reconnect=False)
        m.stop()


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
            streamio_thread = controller.thread

            deadline_ok = m.wait_until(lambda: controller.stream is None, timeout=2.0)
            assert deadline_ok, "controller must give up the link once the machine closes it"
            assert controller.comms.frame_confirmed is False

            # _close_inline must stop streamIO itself (stopRun()), not rely
            # on some other test's close() happening to clear the shared
            # stop Event first — join it directly, on its own, here.
            streamio_thread.join(timeout=2.0)
            assert not streamio_thread.is_alive()
            assert controller._manual_disconnect is True

            assert mock_clock.schedule_once.called
            scheduled = [call.args[0] for call in mock_clock.schedule_once.call_args_list]
            for fn in scheduled:
                fn(0)
            fake_app.root.show_machine_busy_before_identify_popup.assert_called_once()
    finally:
        controller.close(allow_reconnect=False)
        m.stop()


class _EmptyReadStream:
    """Mimics a transport whose recv() returns b"" without the link having
    closed — e.g. pyserial's read() timing out with nothing available.
    Regression double for the busy-before-identify check, which must never
    treat this as the accepted-then-closed busy signature outside WiFi.
    """

    def __init__(self):
        self.closed = False

    def waiting_for_recv(self):
        return True

    def recv(self):
        return b""

    def send(self, data):
        pass

    def close(self):
        self.closed = True


def test_usb_empty_read_does_not_trigger_busy_before_identify(controller):
    """A USB link's recv() returning b"" is not a close signal (unlike a
    TCP socket's), so it must never trip the busy-before-identify check —
    that would sever a perfectly good USB connection on an ordinary read
    timeout, before its first status reply."""
    controller.connection_type = CONN_USB
    stream = _EmptyReadStream()
    controller.stream = stream
    controller.stop.clear()
    controller.thread = threading.Thread(target=controller.streamIO)
    controller.thread.start()
    try:
        time.sleep(0.3)
        assert controller.stream is stream
        assert stream.closed is False
    finally:
        controller.stop.set()
        controller.thread.join(timeout=1.0)
        controller.stop.clear()
        controller.stream = None
