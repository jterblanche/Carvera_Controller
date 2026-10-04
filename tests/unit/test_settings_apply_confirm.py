"""Controller.set_config_value_and_wait, and the settings page's Apply
path built on it (main.py's apply_machine_setting_changes/
_finish_apply_machine_setting_changes).

Applying a setting must not report success unless it actually reached the
card: a refusal because another controller holds control, or a dropped
link, must not look identical to success. These pin:

  - a per-key reply is awaited, with a timeout, and matched against the
    firmware's own success/failure text (Controller-level, against a real
    socket via FakeMachine);
  - the settings page reports "Settings applied" only when every key
    succeeded, otherwise names the failed settings and the firmware's
    reason and reverts just those widgets;
  - config.txt is always re-read afterwards, success or partial failure;
  - the wait happens off the Kivy main thread.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import PTYPE_CTRL_MULTI
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


def _wait_identified(machine_, controller_, timeout=2.0):
    return machine_.wait_until(
        lambda: controller_._hello is not None and controller_._hello.identified, timeout=timeout
    )


def _connect(machine_fixture, controller_, **machine_kwargs):
    m = machine_fixture(mode="new", **machine_kwargs)
    controller_.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller_)
    return m


# -- Controller.set_config_value_and_wait, against a real socket -----------


def test_success_on_the_firmwares_has_been_set_to_line(machine, controller):
    m = _connect(machine, controller)
    result = {}

    def _run():
        result["value"] = controller.set_config_value_and_wait("multi_client.mode", "single_user", timeout=2.0)

    t = threading.Thread(target=_run)
    t.start()
    assert m.wait_until(lambda: len(m.frames_of_type(PTYPE_CTRL_MULTI)) > 0)
    m.send_normal_info(b"sd: multi_client.mode has been set to single_user\r\n")
    t.join(timeout=2.0)
    assert not t.is_alive()

    assert result["value"] == (True, "sd: multi_client.mode has been set to single_user")


def test_failure_on_the_control_refusal(machine, controller):
    m = _connect(machine, controller)
    result = {}

    def _run():
        result["value"] = controller.set_config_value_and_wait("multi_client.mode", "single_user", timeout=2.0)

    t = threading.Thread(target=_run)
    t.start()
    assert m.wait_until(lambda: len(m.frames_of_type(PTYPE_CTRL_MULTI)) > 0)
    m.send_normal_info(b"error:Refused -- Other PC has control\r\n")
    t.join(timeout=2.0)
    assert not t.is_alive()

    success, message = result["value"]
    assert success is False
    assert "Refused" in message
    assert "Other PC" in message


def test_failure_on_timeout_when_nothing_replies(machine, controller):
    _connect(machine, controller)

    start = time.monotonic()
    success, message = controller.set_config_value_and_wait("multi_client.mode", "single_user", timeout=0.3)
    elapsed = time.monotonic() - start

    assert success is False
    assert "no reply" in message
    assert elapsed >= 0.3


def test_a_different_keys_reply_does_not_resolve_this_wait(machine, controller):
    m = _connect(machine, controller)
    result = {}

    def _run():
        result["value"] = controller.set_config_value_and_wait(
            "multi_client.passive_rights", "watch_stop_upload", timeout=2.0
        )

    t = threading.Thread(target=_run)
    t.start()
    assert m.wait_until(lambda: len(m.frames_of_type(PTYPE_CTRL_MULTI)) > 0)
    m.send_normal_info(b"sd: multi_client.mode has been set to single_user\r\n")
    time.sleep(0.1)
    assert t.is_alive(), "a reply naming a different key must not resolve this wait"

    m.send_normal_info(b"sd: multi_client.passive_rights has been set to watch_stop_upload\r\n")
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert result["value"] == (True, "sd: multi_client.passive_rights has been set to watch_stop_upload")


# -- main.py's apply_machine_setting_changes --------------------------------


def _widget(section, key, value):
    return SimpleNamespace(section=section, key=key, value=value)


class _FakeConfigPopup:
    def __init__(self, widgets, originals):
        self._widgets = widgets
        self._widget_snapshot = dict(originals)
        self.btn_apply = SimpleNamespace(disabled=False)

    def _all_setting_items(self):
        return list(self._widgets)

    def get_original(self, section, key):
        return self._widget_snapshot.get((section, key))


def _makera(pending, widgets, originals, controller_):
    from carveracontroller.main import Makera

    root = Makera.__new__(Makera)
    root.setting_change_list = dict(pending)
    root.controller = controller_
    root.config_popup = _FakeConfigPopup(widgets, originals)
    root.message_popup = MagicMock()
    root.download_config_file = MagicMock()
    return root


def test_all_keys_acknowledged_reports_success(monkeypatch):
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, t=0: cb(0))
    controller_ = MagicMock()
    controller_.set_config_value_and_wait.return_value = (True, "sd: multi_client.mode has been set to single_user")
    widgets = [_widget("Machine - Basic", "multi_client.mode", "single_user")]
    originals = {("Machine - Basic", "multi_client.mode"): "multi_user"}
    root = _makera({"multi_client.mode": "single_user"}, widgets, originals, controller_)

    root.apply_machine_setting_changes()

    assert root.setting_change_list == {}
    assert root.config_popup.btn_apply.disabled is True
    root.message_popup.open.assert_called_once()
    assert "applied" in root.message_popup.lb_content.text
    assert widgets[0].value == "single_user"
    assert root.config_popup._widget_snapshot[("Machine - Basic", "multi_client.mode")] == "single_user"
    root.download_config_file.assert_called_once()


def test_one_refused_names_it_and_reverts_the_widget(monkeypatch):
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, t=0: cb(0))
    controller_ = MagicMock()
    controller_.set_config_value_and_wait.return_value = (False, "error:Refused -- Other PC has control")
    widgets = [_widget("Machine - Basic", "multi_client.mode", "single_user")]
    originals = {("Machine - Basic", "multi_client.mode"): "multi_user"}
    root = _makera({"multi_client.mode": "single_user"}, widgets, originals, controller_)

    root.apply_machine_setting_changes()

    text = root.message_popup.lb_content.text
    assert "multi_client.mode" in text
    assert "Refused" in text
    assert "Other PC" in text
    assert widgets[0].value == "multi_user"
    assert root.config_popup._widget_snapshot[("Machine - Basic", "multi_client.mode")] == "multi_user"
    root.download_config_file.assert_called_once()


def test_timeout_is_reported_as_a_failure_and_reverts_the_widget(monkeypatch):
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, t=0: cb(0))
    controller_ = MagicMock()
    controller_.set_config_value_and_wait.return_value = (False, "no reply from the machine within 3s")
    widgets = [_widget("Machine - Basic", "multi_client.mode", "single_user")]
    originals = {("Machine - Basic", "multi_client.mode"): "multi_user"}
    root = _makera({"multi_client.mode": "single_user"}, widgets, originals, controller_)

    root.apply_machine_setting_changes()

    text = root.message_popup.lb_content.text
    assert "multi_client.mode" in text
    assert "no reply" in text
    assert widgets[0].value == "multi_user"


def test_rereads_config_after_apply_even_on_partial_failure(monkeypatch):
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, t=0: cb(0))
    controller_ = MagicMock()
    controller_.set_config_value_and_wait.side_effect = [
        (True, "sd: a has been set to 1"),
        (False, "error:Refused -- Other PC has control"),
    ]
    widgets = [
        _widget("Machine - Basic", "a", "1"),
        _widget("Machine - Basic", "b", "2"),
    ]
    originals = {
        ("Machine - Basic", "a"): "0",
        ("Machine - Basic", "b"): "1",
    }
    root = _makera({"a": "1", "b": "2"}, widgets, originals, controller_)

    root.apply_machine_setting_changes()

    root.download_config_file.assert_called_once()
    assert widgets[0].value == "1"  # succeeded: left as applied
    assert widgets[1].value == "1"  # failed: reverted to the card's value


def test_apply_does_not_block_the_calling_thread(monkeypatch):
    """Guards against the worker's per-key wait (CONFIG_SET_REPLY_TIMEOUT_S)
    freezing the Kivy main thread: apply_machine_setting_changes must
    return immediately, doing the waiting on a background thread."""
    released = threading.Event()

    def _slow_wait(key, value, timeout=3.0):
        released.wait(2.0)
        return True, "sd: %s has been set to %s" % (key, value)

    controller_ = MagicMock()
    controller_.set_config_value_and_wait.side_effect = _slow_wait
    widgets = [_widget("Machine - Basic", "multi_client.mode", "single_user")]
    originals = {("Machine - Basic", "multi_client.mode"): "multi_user"}
    root = _makera({"multi_client.mode": "single_user"}, widgets, originals, controller_)

    start = time.monotonic()
    root.apply_machine_setting_changes()
    elapsed = time.monotonic() - start

    assert elapsed < 0.2
    released.set()
    time.sleep(0.1)  # let the worker thread finish before the test exits
