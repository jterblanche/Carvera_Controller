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
  - config.txt is always re-read afterwards, success or partial failure,
    and when any setting was sent the result is shown only once that
    read-back has ended: a read-back that fails says the values were sent
    but not confirmed and offers to read again;
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
    assert widgets[0].value == "single_user"
    assert root.config_popup._widget_snapshot[("Machine - Basic", "multi_client.mode")] == "single_user"
    root.download_config_file.assert_called_once()
    # "applied" waits for config.txt to be read back.
    root.message_popup.open.assert_not_called()
    assert root._apply_readback.sent == ["multi_client.mode"]

    root._report_apply_readback(root._apply_readback, True)

    root.message_popup.open.assert_called_once()
    assert "applied" in root.message_popup.lb_content.text


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


# -- Reading config.txt back after Apply ------------------------------------
#
# Apply re-reads /sd/config.txt to confirm what the card now holds. When the
# writes were acknowledged but that read fails for a reason of its own (the
# machine ends the transfer, refuses it, it arrives damaged), the settings
# page must say the values were sent but not confirmed and offer to read
# again -- not report a config load error, and not claim they are applied.
# These run the real Apply finish, download_config_file, doDownload and
# finishLoadConfig with only the link and the widgets faked.

REFUSAL = "error:Refused -- Other PC has control"


class _ImmediateThread:
    """Runs the target on .start() instead of in a new thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _readback_host(tmp_path, monkeypatch, download_results):
    """A Makera whose machine acknowledges every config-set and whose
    config.txt downloads return ``download_results`` in turn: None for a
    failed transfer, or a callable that writes the file and returns its
    size."""
    from carveracontroller.main import Makera

    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, t=0: cb(0))
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)

    results = list(download_results)

    def _download(tmp_filename, md5, progress_cb):
        result = results.pop(0)
        return result(tmp_filename) if callable(result) else result

    modem = SimpleNamespace(last_file_error=None, download_md5_failed=False)
    controller_ = MagicMock()
    controller_.set_config_value_and_wait.return_value = (True, "sd: multi_client.mode has been set to single_user")
    controller_.comms.uses_framed_transfer = True
    controller_.downloadCommand.return_value = True
    controller_.connection_type = CONN_WIFI
    controller_.connection_address = "192.0.2.10"
    controller_.stream.modem = modem
    controller_.stream.download.side_effect = _download

    widgets = [_widget("Machine - Basic", "multi_client.mode", "single_user")]
    originals = {("Machine - Basic", "multi_client.mode"): "multi_user"}
    root = _makera({"multi_client.mode": "single_user"}, widgets, originals, controller_)
    del root.download_config_file  # use the real one
    root.temp_dir = str(tmp_path)
    root.fw_version = "1.0"
    root.heartbeat_time = 0
    root._config_download_failures = 0
    root._config_apply_failed = False
    root._selected_file_machine_key = "wifi:192.0.2.10"
    root.confirm_popup = MagicMock()
    root.show_message_popup = MagicMock()
    for name in (
        "progressStart",
        "progressUpdate",
        "progressFinish",
        "updateStatus",
        "load_machine_config_defaults",
        "load_coordinates",
        "load_laser_offsets",
        "attempt_usb_baud_upgrade_if_eligible",
    ):
        setattr(root, name, MagicMock())
    root.load_machine_config = MagicMock(return_value=True)
    root.setting_list = {}
    root.gcode_viewer = SimpleNamespace(high_precision_time_estimate=False)
    return root, controller_, modem


def _config_file(tmp_filename):
    with open(tmp_filename, "w", encoding="utf-8") as handle:
        handle.write("multi_client.mode single_user\n")
    return 30


def _everything_shown(root, controller_):
    """All text the settings page put in front of the user: popups and
    console lines."""
    shown = [str(c.args[0]) for c in root.show_message_popup.call_args_list]
    shown += [str(c.args[0][1]) for c in controller_.log.put.call_args_list]
    if root.message_popup.open.called:
        shown.append(str(root.message_popup.lb_content.text))
    if root.confirm_popup.open.called:
        shown.append(str(root.confirm_popup.lb_content.text))
    return shown


def test_readback_ended_by_the_machine_is_not_reported_as_a_load_error(tmp_path, monkeypatch):
    """The machine acknowledges the write, then ends the read-back transfer
    with no reason (it is already serving another controller's transfer)."""
    root, controller_, _modem = _readback_host(tmp_path, monkeypatch, [None])

    root.apply_machine_setting_changes()

    shown = _everything_shown(root, controller_)
    assert not any("Download config file error" in text for text in shown), shown
    assert not any("Error loading" in text for text in shown), shown
    assert not any("Settings applied" in text for text in shown), shown
    root.confirm_popup.open.assert_called_once()
    text = root.confirm_popup.lb_content.text
    assert "sent to the machine" in text.lower()
    assert "could not be confirmed" in text
    assert "multi_client.mode" in text
    assert root.confirm_popup.confirm_text == "Check again"


def test_readback_refusal_names_the_machines_reason(tmp_path, monkeypatch):
    root, controller_, modem = _readback_host(tmp_path, monkeypatch, [None])

    def _refused(tmp_filename, md5, progress_cb):
        # The transfer fails (returns None) with the refusal recorded.
        modem.last_file_error = REFUSAL

    controller_.stream.download.side_effect = _refused

    root.apply_machine_setting_changes()

    shown = _everything_shown(root, controller_)
    assert not any("Download config file error" in text for text in shown), shown
    assert REFUSAL in root.confirm_popup.lb_content.text


def test_check_again_reads_back_once_more_and_reports_applied_when_it_succeeds(tmp_path, monkeypatch):
    root, controller_, _modem = _readback_host(tmp_path, monkeypatch, [None, _config_file])

    root.apply_machine_setting_changes()
    root.confirm_popup.open.assert_called_once()
    assert not root.message_popup.open.called

    root.confirm_popup.confirm()  # the "Check again" button

    assert controller_.stream.download.call_count == 2
    root.message_popup.open.assert_called_once()
    assert "Settings applied" in root.message_popup.lb_content.text
    root.confirm_popup.open.assert_called_once()  # not shown a second time


def test_check_again_that_fails_again_offers_another_check(tmp_path, monkeypatch):
    root, controller_, _modem = _readback_host(tmp_path, monkeypatch, [None, None])

    root.apply_machine_setting_changes()
    root.confirm_popup.confirm()

    assert root.confirm_popup.open.call_count == 2
    shown = _everything_shown(root, controller_)
    assert not any("Download config file error" in text for text in shown), shown
    assert not root.message_popup.open.called


def test_readback_that_succeeds_reports_applied(tmp_path, monkeypatch):
    root, controller_, _modem = _readback_host(tmp_path, monkeypatch, [_config_file])

    root.apply_machine_setting_changes()

    root.message_popup.open.assert_called_once()
    assert "Settings applied" in root.message_popup.lb_content.text
    root.confirm_popup.open.assert_not_called()
    root.load_machine_config.assert_called_once()


def test_closing_the_offer_leaves_later_config_loads_alone(tmp_path, monkeypatch):
    """After the user declines to check again, a config load that is not
    an Apply read-back reports its own outcome as it always has."""
    root, controller_, _modem = _readback_host(tmp_path, monkeypatch, [None, None])

    root.apply_machine_setting_changes()
    root.confirm_popup.open.assert_called_once()

    root.download_config_file()

    root.confirm_popup.open.assert_called_once()
    shown = [str(c.args[0]) for c in root.show_message_popup.call_args_list]
    assert any("Download config file error" in text for text in shown), shown


def test_config_load_failure_outside_apply_is_still_reported(tmp_path, monkeypatch):
    root, controller_, _modem = _readback_host(tmp_path, monkeypatch, [None])

    root.download_config_file()

    root.confirm_popup.open.assert_not_called()
    shown = [str(c.args[0]) for c in root.show_message_popup.call_args_list]
    assert any("Download config file error" in text for text in shown), shown
