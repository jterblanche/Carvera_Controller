"""The settings page's machine-write paths (Apply, Restore, Save As
Default) while the machine is busy.

Busy means the machine's reported state is anything other than Idle,
Alarm or Sleep (carveracontroller.machine.busy_state.machine_is_busy).
While busy:

  - Makera.refresh_settings_apply_button disables the Apply button and
    shows a short reason, even when nothing about control blocks it.
  - Makera.refuse_machine_settings_write (gating Restore and Save As
    Default -- see main.py's on_config_change) refuses and shows the same
    reason, regardless of who holds control.
  - ConfigPopup._apply_changes does not send a write it would otherwise
    send.

And it must track a settings page left open: calling
refresh_settings_apply_button again after the machine's state changes
updates the button and reason without the page having been reopened --
the same thing app.bind(state=...) does for a running app (see
Makera._bind_settings_busy_gate).

Firmware without a matching busy gate (not merged, or a race) is
unaffected: these paths depend only on app.state, never on a reply, and
set_config_value_and_wait already treats any "error:" reply as failure
regardless of its text -- test_failure_on_the_busy_refusal pins that for
the exact text the gate sends.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.main import ConfigPopup, Makera
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")


@pytest.fixture(autouse=True)
def identity_translations():
    # Keep messages in English regardless of the host locale, same as
    # test_halt_estop_note.py.
    with patch("carveracontroller.main.tr._", side_effect=lambda s, *a: s):
        yield


class _FakeConfigPopup:
    def __init__(self):
        self.btn_apply = SimpleNamespace(disabled=False)
        self.apply_blocked_text = ""


def _makera(state, can_write_machine_settings, has_machine_changes=True):
    root = Makera.__new__(Makera)
    root.config_popup = _FakeConfigPopup()
    root.controller = MagicMock()
    root.controller.can_write_machine_settings = can_write_machine_settings
    root.controller.control_holder_name = "Office PC"
    root.controller_setting_change_list = {}
    root.setting_change_list = {"multi_client.mode": "single_user"} if has_machine_changes else {}
    root.show_message_popup = MagicMock()
    app = SimpleNamespace(state=state)
    patcher = patch("carveracontroller.main.App.get_running_app", return_value=app)
    patcher.start()
    return root, patcher


# -- refresh_settings_apply_button ------------------------------------------


def test_apply_is_disabled_while_busy_even_with_control():
    root, patcher = _makera("Run", can_write_machine_settings=True)
    try:
        root.refresh_settings_apply_button()
        assert root.config_popup.btn_apply.disabled is True
        assert "busy" in root.config_popup.apply_blocked_text
    finally:
        patcher.stop()


@pytest.mark.parametrize("state", ["Run", "Hold", "Home", "Wait", "Tool", "Pause"])
def test_apply_is_disabled_for_every_busy_state(state):
    root, patcher = _makera(state, can_write_machine_settings=True)
    try:
        root.refresh_settings_apply_button()
        assert root.config_popup.btn_apply.disabled is True
    finally:
        patcher.stop()


@pytest.mark.parametrize("state", ["Idle", "Alarm", "Sleep"])
def test_apply_is_enabled_while_idle_like_with_control(state):
    root, patcher = _makera(state, can_write_machine_settings=True)
    try:
        root.refresh_settings_apply_button()
        assert root.config_popup.btn_apply.disabled is False
        assert root.config_popup.apply_blocked_text == ""
    finally:
        patcher.stop()


def test_apply_button_updates_live_when_state_changes_on_an_open_page():
    """A settings page opened while Idle and left open: the button must
    follow a later state change without the page being reopened."""
    root, patcher = _makera("Idle", can_write_machine_settings=True)
    try:
        root.refresh_settings_apply_button()
        assert root.config_popup.btn_apply.disabled is False

        patcher.stop()
        app = SimpleNamespace(state="Run")
        patcher = patch("carveracontroller.main.App.get_running_app", return_value=app)
        patcher.start()
        root.refresh_settings_apply_button()
        assert root.config_popup.btn_apply.disabled is True
        assert "busy" in root.config_popup.apply_blocked_text

        patcher.stop()
        app = SimpleNamespace(state="Idle")
        patcher = patch("carveracontroller.main.App.get_running_app", return_value=app)
        patcher.start()
        root.refresh_settings_apply_button()
        assert root.config_popup.btn_apply.disabled is False
        assert root.config_popup.apply_blocked_text == ""
    finally:
        patcher.stop()


def test_apply_still_blocked_by_control_once_not_busy():
    root, patcher = _makera("Idle", can_write_machine_settings=False)
    try:
        root.refresh_settings_apply_button()
        assert root.config_popup.btn_apply.disabled is True
        assert "Office PC" in root.config_popup.apply_blocked_text
        assert "busy" not in root.config_popup.apply_blocked_text
    finally:
        patcher.stop()


# -- refuse_machine_settings_write (Restore / Save As Default) -------------


def test_restore_and_default_are_refused_while_busy_even_with_control():
    root, patcher = _makera("Home", can_write_machine_settings=True, has_machine_changes=False)
    try:
        assert root.refuse_machine_settings_write() is True
        root.show_message_popup.assert_called_once()
        (text, _flag), _kwargs = root.show_message_popup.call_args
        assert "busy" in text
    finally:
        patcher.stop()


def test_restore_and_default_allowed_while_idle_with_control():
    root, patcher = _makera("Idle", can_write_machine_settings=True, has_machine_changes=False)
    try:
        assert root.refuse_machine_settings_write() is False
        root.show_message_popup.assert_not_called()
    finally:
        patcher.stop()


def test_restore_and_default_still_refused_by_control_once_not_busy():
    root, patcher = _makera("Sleep", can_write_machine_settings=False, has_machine_changes=False)
    try:
        assert root.refuse_machine_settings_write() is True
        (text, _flag), _kwargs = root.show_message_popup.call_args
        assert "Office PC" in text
    finally:
        patcher.stop()


# -- ConfigPopup._apply_changes: the Apply button's own handler ------------


def test_apply_changes_does_not_send_a_write_while_busy():
    popup = ConfigPopup.__new__(ConfigPopup)
    popup._all_setting_items = MagicMock(return_value=[])
    popup._widget_snapshot = {}
    root = SimpleNamespace(
        setting_change_list={"multi_client.mode": "single_user"},
        controller=SimpleNamespace(can_write_machine_settings=True),
        refresh_settings_apply_button=MagicMock(),
        apply_setting_changes=MagicMock(),
    )
    app = SimpleNamespace(state="Run", root=root)
    with patch("carveracontroller.main.App.get_running_app", return_value=app):
        ConfigPopup._apply_changes(popup)

    root.apply_setting_changes.assert_not_called()
    root.refresh_settings_apply_button.assert_called_once()


def test_apply_changes_sends_the_write_while_idle():
    popup = ConfigPopup.__new__(ConfigPopup)
    popup._all_setting_items = MagicMock(return_value=[])
    popup._widget_snapshot = {}
    root = SimpleNamespace(
        setting_change_list={"multi_client.mode": "single_user"},
        controller=SimpleNamespace(can_write_machine_settings=True),
        refresh_settings_apply_button=MagicMock(),
        apply_setting_changes=MagicMock(),
    )
    app = SimpleNamespace(state="Idle", root=root)
    with (
        patch("carveracontroller.main.App.get_running_app", return_value=app),
        patch("carveracontroller.main.Config.write"),
    ):
        ConfigPopup._apply_changes(popup)

    root.apply_setting_changes.assert_called_once()


# -- The firmware's busy refusal reaches the Apply path as a failure -------


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


def test_failure_on_the_busy_refusal(machine, controller):
    """ConfigWriteGate's exact reply text must not be
    mistaken for success -- set_config_value_and_wait already treats any
    "error:" line as a failure, regardless of its wording, so this pins
    that it also catches this specific message."""
    import threading

    from carveracontroller.protocols.framing import PTYPE_CTRL_MULTI

    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)
    result = {}

    def _run():
        result["value"] = controller.set_config_value_and_wait("multi_client.mode", "single_user", timeout=2.0)

    t = threading.Thread(target=_run)
    t.start()
    assert m.wait_until(lambda: len(m.frames_of_type(PTYPE_CTRL_MULTI)) > 0)
    m.send_normal_info(b"error:Refused -- can't change settings while the machine is busy\r\n")
    t.join(timeout=2.0)
    assert not t.is_alive()

    success, message = result["value"]
    assert success is False
    assert "busy" in message
