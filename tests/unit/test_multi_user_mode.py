"""End-to-end tests for multi-user mode — against a real Controller and a
real socket (FakeMachine), the same style as test_passive_state.py.

Covers the acceptance criteria of "Controller: multi-user mode":

  1. A release action, sent only when this controller currently holds
     control on a machine configured for multi-user mode — never
     accidentally, never when there is nothing to release.
  2. A refusal naming the holder is shown the same way a single-user
     refusal already is (#23): as an ordinary text error reply, surfaced by
     the existing generic error handling. Actions the passive-rights level
     allows (pause, stop, upload) are sent the same way any command is,
     never held back by this controller.
  3. The mode reported by the machine's hello ack is reflected in
     Controller.control_mode/multi_user_mode.
"""

from __future__ import annotations

import time

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import PTYPE_CTRL_MULTI, PTYPE_CTRL_RELEASE, PTYPE_FILE_START
from carveracontroller.protocols.handshake import HELLO_MODE_MULTI_USER, HELLO_MODE_SINGLE_USER
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


def _command_payloads(machine_, ptype):
    return list(machine_.frames_of_type(ptype))


# -- Mode is read from the hello ack ----------------------------------------


def test_single_user_mode_is_the_starting_and_default_state(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_SINGLE_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert controller.control_mode == HELLO_MODE_SINGLE_USER
    assert controller.multi_user_mode is False


def test_multi_user_mode_is_read_from_the_hello_ack(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert controller.control_mode == HELLO_MODE_MULTI_USER
    assert controller.multi_user_mode is True


def test_old_firmware_never_reports_a_mode_and_stays_single_user(machine, controller):
    """Old firmware never sends a hello ack at all, so this connection is
    never subscribed — the same "must behave exactly as it always has" rule
    passive state already applies to has_control applies here too."""
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.resolved)

    assert controller.control_mode == HELLO_MODE_SINGLE_USER
    assert controller.multi_user_mode is False


# -- The release action: deliberate, and only when there is something to release --


def test_can_release_control_is_false_in_single_user_mode_even_with_control(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_SINGLE_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)

    # Single-user mode has nothing to release: the next user-caused command
    # from anywhere already takes control there, same as always.
    assert controller.can_release_control is False


def test_can_release_control_is_false_in_multi_user_mode_without_control(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(OTHER_ID, b"Office PC")
    assert m.wait_until(lambda: controller.control_holder_id == OTHER_ID)

    assert controller.has_control is False
    assert controller.can_release_control is False


def test_can_release_control_is_true_once_holding_control_in_multi_user_mode(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)

    assert controller.can_release_control is True


def test_release_control_sends_the_release_frame_when_holding_control(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)

    assert controller.release_control() is True

    assert m.wait_until(lambda: len(_command_payloads(m, PTYPE_CTRL_RELEASE)) >= 1)
    assert _command_payloads(m, PTYPE_CTRL_RELEASE)[0] == b""


def test_release_control_does_nothing_without_control(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert controller.has_control is False

    assert controller.release_control() is False
    time.sleep(0.2)  # give any (wrongly) sent frame time to arrive
    assert len(_command_payloads(m, PTYPE_CTRL_RELEASE)) == 0


def test_release_control_does_nothing_in_single_user_mode(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_SINGLE_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())
    assert m.wait_until(lambda: controller.has_control is True)

    assert controller.release_control() is False
    time.sleep(0.2)
    assert len(_command_payloads(m, PTYPE_CTRL_RELEASE)) == 0


# -- A refusal naming the holder; allowed actions are sent unconditionally --


def test_a_not_holder_refusal_naming_the_holder_is_shown(machine, controller):
    """Multi-user mode's "someone else has control" refusal is, like the
    single-user motion refusal it sits beside, an ordinary text error reply
    — so the existing generic error handling (Controller.parseLine) already
    surfaces it with no new decoding needed here."""
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    reason = b"error:Transfer refused -- Office PC has control\r\n"
    assert m.send_normal_info(reason)

    seen = []

    def _check():
        seen.extend(_drain_log_messages(controller))
        return any(
            kind == Controller.MSG_ERROR and "Transfer refused -- Office PC has control" in text for kind, text in seen
        )

    assert m.wait_until(_check, timeout=2.0)


def test_pause_and_stop_are_sent_regardless_of_holding_control(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert controller.has_control is False

    controller.suspendCommand()
    controller.abortCommand()

    assert m.wait_until(lambda: any(b"suspend" in p for p in m.frames_of_type(PTYPE_CTRL_MULTI)))
    assert m.wait_until(lambda: any(b"abort" in p for p in m.frames_of_type(PTYPE_CTRL_MULTI)))


def test_upload_is_sent_regardless_of_holding_control(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert controller.has_control is False

    assert controller.uploadCommand("job.nc") is True

    assert m.wait_until(lambda: any(b"upload job.nc" in p for p in _command_payloads(m, PTYPE_FILE_START)))


# -- Machine settings: writable unless another controller holds control ----


def test_machine_settings_are_not_writable_while_another_holds_control_in_multi_user_mode(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(OTHER_ID, b"Office PC")
    assert m.wait_until(lambda: controller.control_holder_id == OTHER_ID)

    assert controller.can_write_machine_settings is False


def test_machine_settings_are_writable_once_holding_control_in_multi_user_mode(machine, controller):
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(OTHER_ID, b"Office PC")
    assert m.wait_until(lambda: controller.can_write_machine_settings is False)
    assert m.send_control_changed_event(IDENTITY.id, IDENTITY.name.encode())

    assert m.wait_until(lambda: controller.can_write_machine_settings is True)


def test_machine_settings_are_writable_in_multi_user_mode_while_nobody_holds_control(machine, controller):
    """With control free, the write itself takes control, so it isn't refused."""
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_MULTI_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert controller.control_holder_id == 0

    assert controller.can_write_machine_settings is True


def test_machine_settings_are_writable_in_single_user_mode_while_another_controller_holds_control(machine, controller):
    """Single-user mode: acting takes control, so nothing is disabled."""
    m = machine(mode="new", hello_ack_mode=HELLO_MODE_SINGLE_USER)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    assert m.send_control_changed_event(OTHER_ID, b"Office PC")
    assert m.wait_until(lambda: controller.control_holder_id == OTHER_ID)

    assert controller.can_write_machine_settings is True


def test_machine_settings_are_writable_against_old_firmware(machine, controller):
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.resolved)

    assert controller.can_write_machine_settings is True
