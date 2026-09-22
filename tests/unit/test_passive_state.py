"""End-to-end tests for passive state — against a real Controller and a
real socket (FakeMachine), the same style as
test_subscribe_and_shared_console.py.

Covers the first four acceptance criteria of "Controller: passive state":

  1. An indicator naming who has control, driven by control-changed events
     (the machine's `0x68` event frame, kind 5) — never guessed from this
     controller's own sends.
  2. Connect-time writes (clock set, lights on connect and disconnect) are
     held back while this controller is passive; reads are unaffected.
  3. A refusal during interactive motion is shown with the firmware's own
     reason text, verbatim.
  4. A reconnect starts passive again (no holder known until a fresh event
     says otherwise), and a pendant-shaped command (jog) goes out on the
     same ordinary, control-affecting channel as any other user action —
     never the automatic-query channel connect-time reads use — so it can
     move control just like any other user-caused command.
"""

from __future__ import annotations

import time

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import PTYPE_AUTO_COMMAND, PTYPE_CTRL_MULTI
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")
OTHER_ID = 0x0807060504030201  # some other controller's id, never ours


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


def _drain_log_messages(controller):
    """All (kind, text) pairs currently queued on controller.log, in order."""
    messages = []
    while True:
        try:
            messages.append(controller.log.get_nowait())
        except Exception:
            break
    return messages


def _wait_identified(machine_, controller_, timeout=2.0):
    return machine_.wait_until(
        lambda: controller_._hello is not None and controller_._hello.identified, timeout=timeout
    )


def _command_payloads(machine_):
    return list(machine_.frames_of_type(PTYPE_CTRL_MULTI))


# -- AC1: the indicator is driven only by control-changed events ------------


def test_starts_with_no_holder_known_until_an_event_arrives(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    # No control-changed event has arrived yet — this controller does not
    # guess, from a fresh connection, who (if anyone) already holds
    # control on a machine that was already in use.
    assert controller.control_holder_id == 0
    assert controller.control_holder_name == ""
    assert controller.has_control is False


def test_control_changed_event_naming_self_is_reflected(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.control_holder_id == IDENTITY.id)

    assert controller.control_holder_name == IDENTITY.name
    assert controller.has_control is True


def test_control_changed_event_naming_someone_else_is_reflected(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.send_control_changed_event(OTHER_ID, b"Office PC")
    assert m.wait_until(lambda: controller.control_holder_id == OTHER_ID)

    assert controller.control_holder_name == "Office PC"
    # Someone else's id, never ours: has_control must read False.
    assert controller.has_control is False


def test_control_changed_event_naming_nobody_clears_the_holder(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)

    # The machine's own "nobody" encoding (e.g. the previous holder's link
    # was reaped) — the same 0/"" this controller starts at.
    assert m.send_control_changed_event(0, b"")
    assert m.wait_until(lambda: controller.control_holder_id == 0)
    assert controller.has_control is False


# -- AC2: connect-time writes suppressed while passive; reads unaffected ----


def test_light_on_at_connect_is_held_back_while_passive(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    controller.apply_session_lights(True, enabled=True)
    time.sleep(0.2)  # give any (wrongly) sent frame time to arrive
    assert not any(b"M821" in payload for payload in _command_payloads(m))
    assert controller._session_lights_applied is False


def test_light_on_at_connect_is_sent_once_this_controller_has_control(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)

    controller.apply_session_lights(True, enabled=True)

    assert m.wait_until(lambda: any(b"M821" in payload for payload in _command_payloads(m)))
    assert controller._session_lights_applied is True


def test_light_off_at_disconnect_is_held_back_while_passive(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    # Never took control on this connection — apply_session_lights(False)
    # is called unconditionally by every close path.
    controller.apply_session_lights(False, enabled=True)
    time.sleep(0.2)
    assert not any(b"M822" in payload for payload in _command_payloads(m))


def test_clock_set_is_held_back_while_passive(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    controller.syncTime()
    time.sleep(0.2)
    assert not any(payload.startswith(b"time ") for payload in _command_payloads(m))


def test_clock_set_is_sent_once_this_controller_has_control(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)

    controller.syncTime()

    assert m.wait_until(lambda: any(payload.startswith(b"time ") for payload in _command_payloads(m)))


def test_connect_time_writes_go_through_unchanged_on_old_firmware(machine, controller):
    """Old firmware never identifies, so this connection is never subscribed
    and has_control never means anything for it — the passive-state gate on
    syncTime/apply_session_lights short-circuits on `_status_subscribed()`
    before it ever looks at has_control, so both writes go out exactly as
    they did before this feature existed. Firmware that does not publish
    must behave exactly as it always has; that is a hard requirement, not
    a nicety."""
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: controller._hello is not None and controller._hello.resolved)
    assert controller._status_subscribed() is False
    assert controller.has_control is False  # never subscribed, so never "in control" either

    controller.apply_session_lights(True, enabled=True)
    controller.syncTime()

    assert m.wait_until(lambda: any(b"M821" in payload for payload in _command_payloads(m)))
    assert m.wait_until(lambda: any(payload.startswith(b"time ") for payload in _command_payloads(m)))


def test_reads_still_sent_while_passive(machine, controller):
    """The connect-time automatic reads (time/version/model/ftype, get wcs)
    are unaffected by passive state: they never moved control in the first
    place (they route through the automatic-command wrapper — see
    Controller._send_automatic_command), so there is nothing to hold back.
    """
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert controller.has_control is False
    controller.queryTime()
    controller.queryVersion()
    controller.viewWCS(automatic=True)

    assert m.wait_until(lambda: len(m.frames_of_type(PTYPE_AUTO_COMMAND)) >= 3)


# -- AC3: a refusal during interactive motion is shown with its reason ------


def test_refusal_reply_is_shown_with_the_firmware_reason_verbatim(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    reason = b"error:Transfer refused -- Office PC has control and an interactive move is in progress\r\n"
    assert m.send_normal_info(reason)

    seen = []

    def _check():
        seen.extend(_drain_log_messages(controller))
        return any(
            kind == Controller.MSG_ERROR
            and "Transfer refused -- Office PC has control and an interactive move is in progress" in text
            for kind, text in seen
        )

    assert m.wait_until(_check, timeout=2.0)


def test_refusal_reply_without_a_named_holder_is_shown_too(machine, controller):
    """The firmware omits the holder's name when nobody currently holds
    control, which can happen while the machine homes at boot before anyone
    has connected — surfaced exactly as sent, same as the named case."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    reason = b"error:Transfer refused -- an interactive move is in progress\r\n"
    assert m.send_normal_info(reason)

    seen = []

    def _check():
        seen.extend(_drain_log_messages(controller))
        return any(
            kind == Controller.MSG_ERROR and "Transfer refused -- an interactive move is in progress" in text
            for kind, text in seen
        )

    assert m.wait_until(_check, timeout=2.0)


# -- AC4: reconnect returns to passive; pendant input counts as acting ------


def test_reconnect_returns_to_passive(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)

    controller.close(allow_reconnect=False)
    assert controller.control_holder_id == 0
    assert controller.control_holder_name == ""
    assert controller.has_control is False

    # Reconnecting to the same machine must not resurrect the old holder —
    # nothing has told this fresh connection who (if anyone) has control.
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert controller.control_holder_id == 0
    assert controller.has_control is False


def test_pendant_shaped_jog_uses_the_ordinary_control_affecting_channel(machine, controller):
    """A pendant press reaches the machine the same way this test does:
    Controller.jog() -> executeCommand() -> the ordinary PTYPE_CTRL_MULTI
    channel (see carveracontroller/addons/pendant/pendant.py) — never the
    automatic-command wrapper connect-time reads use, and never held back
    by has_control the way apply_session_lights/syncTime are. That is what
    lets it move control on the machine like any other user action."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert controller.has_control is False

    controller.jog("X1")

    assert m.wait_until(lambda: any(b"$J X1" in payload for payload in _command_payloads(m)))

    # The real firmware would now publish a control-changed event naming
    # this controller, since the jog just sent was a user-caused command
    # from a client that did not already hold control.
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)
