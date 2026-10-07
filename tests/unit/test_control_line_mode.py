"""The control line states the machine's running mode.

The line under the status button says who has control and, once the hello
ack has reported it, whether the machine runs in single-user or multi-user
mode: "You have control · multi-user", "Office PC has control ·
single-user", "No one has control · multi-user". The two modes treat
another controller's commands differently (control is taken over in
single-user mode, refused in multi-user mode), so the line says which rules
apply. Before the ack resolves, and on old firmware, which has no modes, no
mode text is shown.

The end-to-end tests run a real Controller against FakeMachine, with a bare
Makera standing in for the screen, and read Makera.control_holder_text.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import carveracontroller.Controller as controller_module
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.hello import ACK_TIMEOUT_S
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.main import Makera
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")
OTHER_ID = 0x0807060504030201
SINGLE_USER = 0
MULTI_USER = 1


class _ScreenClock:
    """Stands in for Kivy's Clock inside Controller: collects the callbacks
    the streamIO thread schedules, so the test thread can run them in order,
    the way Kivy's main loop would."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = []

    def schedule_once(self, callback, timeout=0):
        with self._lock:
            self._pending.append(callback)

    def run_pending(self):
        with self._lock:
            pending, self._pending = self._pending, []
        for callback in pending:
            callback(0)


def _screen(controller_):
    root = Makera.__new__(Makera)
    root.controller = controller_
    root.identity = IDENTITY
    root.status_drop_down = MagicMock()
    root.refresh_settings_apply_button = MagicMock()
    root.update_connected_controllers = MagicMock()
    root.announce_client_presence = MagicMock()
    return root


@pytest.fixture
def controller():
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    yield c
    c.close(allow_reconnect=False)


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
def screen(controller):
    root = _screen(controller)
    clock = _ScreenClock()
    with (
        patch.object(controller_module, "App") as app_cls,
        patch.object(controller_module, "Clock", clock),
    ):
        app_cls.get_running_app.return_value = SimpleNamespace(root=root)
        yield root, clock


def _line_reads(root, clock, text):
    def predicate():
        clock.run_pending()
        return root.control_holder_text == text

    return predicate


def test_nobody_in_control_on_a_multi_user_machine(machine, controller, screen):
    root, clock = screen
    m = machine(mode="new", hello_ack_mode=MULTI_USER, client_list_entries=[(OTHER_ID, "Office PC", 0, False)])
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(_line_reads(root, clock, "No one has control · multi-user"), timeout=2.0)


def test_another_controller_in_control_on_a_single_user_machine(machine, controller, screen):
    root, clock = screen
    m = machine(mode="new", hello_ack_mode=SINGLE_USER, client_list_entries=[(OTHER_ID, "Office PC", 0, True)])
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(_line_reads(root, clock, "Office PC has control · single-user"), timeout=2.0)


def test_this_controller_taking_control_keeps_the_mode(machine, controller, screen):
    root, clock = screen
    m = machine(mode="new", hello_ack_mode=MULTI_USER, client_list_entries=[(OTHER_ID, "Office PC", 0, False)])
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(_line_reads(root, clock, "No one has control · multi-user"), timeout=2.0)

    m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())

    assert m.wait_until(_line_reads(root, clock, "You have control · multi-user"), timeout=2.0)


def test_reconnecting_to_a_machine_in_another_mode_shows_the_new_mode(machine, controller, screen):
    root, clock = screen
    first = machine(mode="new", hello_ack_mode=MULTI_USER, client_list_entries=[(OTHER_ID, "Office PC", 0, False)])
    controller.open(CONN_WIFI, first.address())
    assert first.wait_until(_line_reads(root, clock, "No one has control · multi-user"), timeout=2.0)

    controller.close(allow_reconnect=False)
    clock.run_pending()
    root.control_holder_text = ""
    second = machine(mode="new", hello_ack_mode=SINGLE_USER, client_list_entries=[(OTHER_ID, "Office PC", 0, False)])
    controller.open(CONN_WIFI, second.address())

    assert second.wait_until(_line_reads(root, clock, "No one has control · single-user"), timeout=2.0)


def test_no_mode_text_before_the_hello_ack_resolves(controller):
    # Not connected, so not identified: whatever reaches the line says who
    # has control and nothing about a mode the machine has not reported.
    root = _screen(controller)

    root.update_control_holder(0, "")

    assert root.control_holder_text == "No one has control"


def test_old_firmware_shows_no_line_and_no_mode(machine, controller, screen):
    root, clock = screen
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(
        lambda: controller._hello is not None and controller._hello.resolved, timeout=ACK_TIMEOUT_S + 2.0
    )
    time.sleep(0.2)
    clock.run_pending()
    assert root.control_holder_text == ""
