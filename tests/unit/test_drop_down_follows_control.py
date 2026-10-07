"""The status drop-down's "in control" mark follows control as it moves.

Each row's mark comes from the controller's connected_clients. Those come
from client-list replies, which the controller requests only when it
identifies and after a client-joined/left event. Control moving between
controllers that stay connected raises a control-changed event and nothing
else, so the rows must take the new holder from that event, without asking
the machine for anything (a client-list request is automatic traffic, and
nothing automatic may ever look like a user acting).

These run a real Controller against FakeMachine, with a bare Makera and a
real StatusDropDown row container standing in for the screen, and read the
row text a user sees.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kivy.uix.boxlayout import BoxLayout

import carveracontroller.Controller as controller_module
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.main import Makera, StatusDropDown
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="PC")
OTHER_ID = 0x0807060504030201
HELLO_MODE_MULTI_USER = 1


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


def _drop_down():
    drop_down = StatusDropDown.__new__(StatusDropDown)
    drop_down.connected_controllers_container = BoxLayout(orientation="vertical")
    return drop_down


def _screen(controller_):
    root = Makera.__new__(Makera)
    root.controller = controller_
    root.identity = IDENTITY
    root.status_drop_down = _drop_down()
    root.refresh_settings_apply_button = MagicMock()
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


def _row_texts(root):
    return sorted(child.text for child in root.status_drop_down.connected_controllers_container.children)


def _rows_read(root, clock, expected):
    def predicate():
        clock.run_pending()
        return _row_texts(root) == sorted(expected)

    return predicate


def _joined(machine_factory, controller_, root, clock):
    m = machine_factory(
        mode="new",
        hello_ack_mode=HELLO_MODE_MULTI_USER,
        client_list_entries=[(OTHER_ID, "Demo", 0, False), (IDENTITY.id, IDENTITY.name, 0, False)],
    )
    controller_.open(CONN_WIFI, m.address())
    assert m.wait_until(_rows_read(root, clock, ["Demo", "PC (you)"]), timeout=2.0)
    return m


def test_taking_control_marks_this_controllers_own_row(machine, controller, screen):
    root, clock = screen
    m = _joined(machine, controller, root, clock)

    m.send_control_changed_event(IDENTITY.id, b"PC")

    assert m.wait_until(_rows_read(root, clock, ["Demo", "PC (you) — in control"]), timeout=2.0)


def test_another_controller_taking_control_moves_the_mark_to_its_row(machine, controller, screen):
    root, clock = screen
    m = _joined(machine, controller, root, clock)
    m.send_control_changed_event(IDENTITY.id, b"PC")
    assert m.wait_until(_rows_read(root, clock, ["Demo", "PC (you) — in control"]), timeout=2.0)

    m.send_control_changed_event(OTHER_ID, b"Demo")

    assert m.wait_until(_rows_read(root, clock, ["Demo — in control", "PC (you)"]), timeout=2.0)


def test_control_released_clears_every_mark(machine, controller, screen):
    root, clock = screen
    m = _joined(machine, controller, root, clock)
    m.send_control_changed_event(OTHER_ID, b"Demo")
    assert m.wait_until(_rows_read(root, clock, ["Demo — in control", "PC (you)"]), timeout=2.0)

    m.send_control_changed_event(0, b"")

    assert m.wait_until(_rows_read(root, clock, ["Demo", "PC (you)"]), timeout=2.0)


def test_control_moving_sends_no_client_list_request(machine, controller, screen):
    root, clock = screen
    m = _joined(machine, controller, root, clock)
    requests_after_join = m.client_list_requests

    m.send_control_changed_event(OTHER_ID, b"Demo")
    assert m.wait_until(_rows_read(root, clock, ["Demo — in control", "PC (you)"]), timeout=2.0)
    time.sleep(0.2)

    assert m.client_list_requests == requests_after_join
