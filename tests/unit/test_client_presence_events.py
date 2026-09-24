"""Tests for announcing another controller joining or leaving the machine.

The decision itself (machine/presence.py) is tested directly; the rest runs
against a real Controller and a real socket (FakeMachine), the same style as
test_tool_table_relay_and_file_events.py.
"""

from __future__ import annotations

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.machine.presence import PresenceAnnouncement, presence_announcement
from carveracontroller.protocols.handshake import ClientPresenceChanged
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")
OTHER_ID = 0x0807060504030201


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


def _wait_identified(machine_, controller_, timeout=2.0):
    return machine_.wait_until(
        lambda: controller_._hello is not None and controller_._hello.identified, timeout=timeout
    )


# -- the decision -------------------------------------------------------------


def test_another_controller_joining_is_announced_by_name():
    event = ClientPresenceChanged(client_id=OTHER_ID, name="Office PC", joined=True)
    assert presence_announcement(event, IDENTITY.id, enabled=True) == PresenceAnnouncement(
        name="Office PC", joined=True
    )


def test_another_controller_leaving_is_announced_by_name():
    event = ClientPresenceChanged(client_id=OTHER_ID, name="Office PC", joined=False)
    assert presence_announcement(event, IDENTITY.id, enabled=True) == PresenceAnnouncement(
        name="Office PC", joined=False
    )


def test_nothing_is_announced_when_turned_off():
    for joined in (True, False):
        event = ClientPresenceChanged(client_id=OTHER_ID, name="Office PC", joined=joined)
        assert presence_announcement(event, IDENTITY.id, enabled=False) is None


def test_this_controller_is_never_announced_to_itself():
    # The machine publishes "joined" to the joining controller too.
    event = ClientPresenceChanged(client_id=IDENTITY.id, name=IDENTITY.name, joined=True)
    assert presence_announcement(event, IDENTITY.id, enabled=True) is None


# -- end to end over a real socket ---------------------------------------------


def test_joined_event_is_announced_and_refreshes_the_client_list(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.wait_until(lambda: m.client_list_requests >= 1)
    requests_before = m.client_list_requests

    assert m.send_client_presence_event(OTHER_ID, b"Office PC", joined=True)

    assert m.wait_until(
        lambda: controller.last_presence_announcement == PresenceAnnouncement(name="Office PC", joined=True)
    )
    assert m.wait_until(lambda: m.client_list_requests > requests_before)


def test_left_event_is_announced(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.send_client_presence_event(OTHER_ID, b"Workshop", joined=False)

    assert m.wait_until(
        lambda: controller.last_presence_announcement == PresenceAnnouncement(name="Workshop", joined=False)
    )


def test_turned_off_announces_nothing_but_still_refreshes_the_list(machine, controller):
    controller.announce_other_controllers = False
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.wait_until(lambda: m.client_list_requests >= 1)
    requests_before = m.client_list_requests

    assert m.send_client_presence_event(OTHER_ID, b"Office PC", joined=True)

    # The list refresh proves the event was received and handled.
    assert m.wait_until(lambda: m.client_list_requests > requests_before)
    assert controller.last_presence_announcement is None


def test_own_joined_event_is_not_announced(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.wait_until(lambda: m.client_list_requests >= 1)
    requests_before = m.client_list_requests

    assert m.send_client_presence_event(IDENTITY.id, IDENTITY.name.encode(), joined=True)

    assert m.wait_until(lambda: m.client_list_requests > requests_before)
    assert controller.last_presence_announcement is None


def test_presence_event_does_not_touch_control(machine, controller):
    """Kind 6 and 7 share kind 5's layout; a joined event naming this
    controller must not be taken for "this controller now has control"."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.send_client_presence_event(IDENTITY.id, IDENTITY.name.encode(), joined=True)
    assert m.send_client_presence_event(OTHER_ID, b"Office PC", joined=True)
    assert m.wait_until(lambda: controller.last_presence_announcement is not None)

    assert controller.control_holder_id == 0
    assert controller.has_control is False


def test_old_firmware_announces_nothing(machine, controller):
    # Old firmware never sends these events; nothing is announced and
    # nothing goes wrong.
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.resolved)

    assert controller._status_subscribed() is False
    assert controller.last_presence_announcement is None
