"""Deriving who holds control from a client-list reply.

Controller._on_client_list used to store the decoded list and notify the
UI without ever looking at each entry's own ``has_control`` flag (the
machine's only source of truth for this was treated as a control-changed
event, 0x68 kind 5). A controller that joined while someone already held
control -- or while nobody did -- showed no "who has control" line until
the next control-changed event, whoever held control at the time. This
covers _on_client_list deriving the holder from the list and, when it
disagrees with the current state, applying it through _on_control_changed
exactly as a control-changed event would -- and never doing so when the
list already agrees, so no spurious UI update fires on every routine
client-list refresh.

The unit tests call Controller._on_client_list directly with a hand-built
client-list-reply payload, the same wire layout
protocols.handshake.decode_client_list reads: count(1) + per entry
id(8, BE) + name_len(1) + name + link(1) + has_control(1). The end-to-end
test at the bottom goes through the real join path instead (a real
Controller against a real socket, FakeMachine -- the same style as
test_passive_state.py), to prove the fix also reaches
Controller._on_hello_ack's own client-list request on identify, not just
the decoder.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")
OTHER_ID = 0x0807060504030201


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


def _client_list_payload(entries):
    """``entries``: iterable of (id, name, link, has_control). Encodes a
    client-list-reply payload in the wire layout decode_client_list
    reads."""
    entries = list(entries)
    payload = bytes([len(entries)])
    for client_id, name, link, has_control in entries:
        name_bytes = name.encode("utf-8")
        payload += (
            client_id.to_bytes(8, "big")
            + bytes([len(name_bytes)])
            + name_bytes
            + bytes([link, 1 if has_control else 0])
        )
    return payload


def _spy_on_control_changed(controller_):
    """Wrap the real _on_control_changed so a test can assert both the
    resulting state and whether it was actually invoked (vs. the state
    already having been that value from a previous call)."""
    controller_._on_control_changed = MagicMock(wraps=controller_._on_control_changed)
    return controller_._on_control_changed


def test_someone_else_holding_control_is_shown_on_join(controller):
    spy = _spy_on_control_changed(controller)
    payload = _client_list_payload([(OTHER_ID, "Office PC", 0, True), (IDENTITY.id, IDENTITY.name, 0, False)])

    controller._on_client_list(payload)

    spy.assert_called_once_with(OTHER_ID, "Office PC")
    assert controller.control_holder_id == OTHER_ID
    assert controller.control_holder_name == "Office PC"
    assert controller.has_control is False


def test_this_controller_holding_control_is_shown_on_join(controller):
    spy = _spy_on_control_changed(controller)
    payload = _client_list_payload([(IDENTITY.id, IDENTITY.name, 0, True), (OTHER_ID, "Office PC", 0, False)])

    # has_control also requires this connection to be subscribed (identified
    # by firmware that understands the handshake) -- true on any real
    # connection that got far enough to receive a client-list reply at all,
    # but this test calls _on_client_list directly, with no connection
    # behind it, so that has to be faked in rather than achieved for real.
    with patch.object(controller, "_status_subscribed", return_value=True):
        controller._on_client_list(payload)

        spy.assert_called_once_with(IDENTITY.id, IDENTITY.name)
        assert controller.control_holder_id == IDENTITY.id
        assert controller.has_control is True


def test_nobody_holding_control_is_shown_when_the_list_says_so(controller):
    # Arrive already believing OTHER_ID holds it (e.g. from an earlier
    # control-changed event), then a client-list reply says nobody does
    # any more -- the same transition a control-changed event naming 0/""
    # would cause.
    controller._on_control_changed(OTHER_ID, "Office PC")
    spy = _spy_on_control_changed(controller)
    payload = _client_list_payload([(OTHER_ID, "Office PC", 0, False), (IDENTITY.id, IDENTITY.name, 0, False)])

    controller._on_client_list(payload)

    spy.assert_called_once_with(0, "")
    assert controller.control_holder_id == 0
    assert controller.control_holder_name == ""
    assert controller.has_control is False


def test_list_agreeing_with_the_current_state_raises_no_spurious_event(controller):
    # Already believes OTHER_ID holds control; a fresh client-list reply
    # (e.g. from a routine client-joined/left refresh) says the same thing
    # -- nothing should be re-applied or re-notified.
    controller._on_control_changed(OTHER_ID, "Office PC")
    spy = _spy_on_control_changed(controller)
    payload = _client_list_payload([(OTHER_ID, "Office PC", 0, True), (IDENTITY.id, IDENTITY.name, 0, False)])

    controller._on_client_list(payload)

    spy.assert_not_called()
    assert controller.control_holder_id == OTHER_ID
    assert controller.control_holder_name == "Office PC"


# -- end to end: the real join path, not just the decoder -------------------


def test_joining_while_another_controller_holds_control_shows_it_without_any_event(machine, controller):
    """The acceptance criterion from the bug report: join while another
    controller already holds control, and the joining controller shows
    "<name> has control" immediately -- from the client-list reply
    Controller._on_hello_ack already requests on identify, well before any
    control-changed event (if one ever arrives at all)."""
    m = machine(mode="new", client_list_entries=[(OTHER_ID, "Office PC", 0, True)])
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: controller.control_holder_id == OTHER_ID, timeout=2.0)
    assert controller.control_holder_name == "Office PC"
    assert controller.has_control is False
