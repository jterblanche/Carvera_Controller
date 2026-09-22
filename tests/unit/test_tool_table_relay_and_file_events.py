"""End-to-end tests for the relayed tool table and the passive file-fetch
events -- against a real Controller and a real socket (FakeMachine), the
same style as test_passive_state.py.

Covers:
  1. This controller can send a tool-table relay once subscribed, and
     never before.
  2. A relay from another identified client is decoded into
     relayed_tool_table; an unrecognised relay payload is dropped.
  3. Upload-finished and play-started events are decoded and recorded
     (last_published_file_path); other event kinds still do not crash.
  4. A fresh reconnect clears both, same as connected_clients/control state.
"""

from __future__ import annotations

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import PTYPE_RELAY
from carveracontroller.protocols.relay import encode_tool_table_relay
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


# -- sending a tool-table relay ----------------------------------------------


def test_send_tool_table_relay_once_subscribed(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert controller.send_tool_table_relay({1: "T1 Flat End Mill"}) is True
    assert m.wait_until(lambda: len(m.frames_of_type(PTYPE_RELAY)) >= 1)
    payload = m.frames_of_type(PTYPE_RELAY)[0]
    assert payload == encode_tool_table_relay({1: "T1 Flat End Mill"})


def test_send_tool_table_relay_held_back_before_subscribed():
    # No connection at all: never subscribed, must not raise or claim success.
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    try:
        assert c.send_tool_table_relay({1: "T1"}) is False
    finally:
        c.close(allow_reconnect=False)


def test_send_tool_table_relay_held_back_on_old_firmware(machine, controller):
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.resolved)
    assert controller._status_subscribed() is False

    assert controller.send_tool_table_relay({1: "T1"}) is False


# -- receiving a relay --------------------------------------------------------


def test_relay_from_another_client_is_decoded(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    payload = encode_tool_table_relay({1: "T1 Flat End Mill", 2: "T2 Ball End Mill"})
    assert m.send_relay(OTHER_ID, payload)

    assert m.wait_until(lambda: controller.relayed_tool_table == {1: "T1 Flat End Mill", 2: "T2 Ball End Mill"})


def test_unrecognised_relay_payload_is_dropped(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    # Some other relay convention this controller does not understand.
    assert m.send_relay(OTHER_ID, b"\x99not a tool table")
    # Give it a moment to (not) be processed, then confirm nothing changed.
    assert m.send_relay(OTHER_ID, encode_tool_table_relay({7: "T7 Drill"}))
    assert m.wait_until(lambda: controller.relayed_tool_table == {7: "T7 Drill"})


# -- receiving upload-finished / play-started events --------------------------


def test_upload_finished_event_is_recorded(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.send_upload_finished_event(b"/sd/job.nc", size=42)
    assert m.wait_until(lambda: controller.last_published_file_path == "/sd/job.nc")


def test_play_started_event_is_recorded(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.send_play_started_event(b"/sd/other.nc")
    assert m.wait_until(lambda: controller.last_published_file_path == "/sd/other.nc")


def test_control_changed_event_still_works_alongside_file_events(machine, controller):
    """The EVENT dispatch now tries three decoders in turn -- a
    control-changed event must still be recognised correctly, not
    swallowed by one of the newer checks."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)
    assert controller.last_published_file_path == ""


# -- reconnect clears both ----------------------------------------------------


def test_reconnect_clears_relayed_table_and_published_path(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_relay(OTHER_ID, encode_tool_table_relay({1: "T1"}))
    assert m.send_upload_finished_event(b"/sd/job.nc")
    assert m.wait_until(lambda: controller.relayed_tool_table == {1: "T1"})
    assert m.wait_until(lambda: controller.last_published_file_path == "/sd/job.nc")

    controller.close(allow_reconnect=False)
    assert controller.relayed_tool_table == {}
    assert controller.last_published_file_path == ""
