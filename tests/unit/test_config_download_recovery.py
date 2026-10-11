"""Loading config.txt from the machine recovers from a failed download.

Found on a Windows PC switched from WiFi to USB: the machine ended the first
config.txt download, a second one started straight away, the serial port was
then closed under it, and the temporary file could not be deleted or
renamed (WinError 32: still open). Each of those failures ended the
download's worker thread with an exception, so the progress popup stayed up,
config_loading stayed set, and pressing Cancel on the popup crashed the app
(ProgressPopup had no cancel()).

These run the real download_config_file, doDownload and finishLoadConfig
with the link, the clock and the widgets faked:

  - a config download that fails is retried only after
    CONFIG_DOWNLOAD_RETRY_DELAY_S, and never while one is still running;
  - a temporary file that cannot be deleted or renamed does not end the
    worker: the download is reported as failed (or saved by copying), the
    popup closes, config_loading is cleared and the next attempt works;
  - a worker that raises anyway still ends its download;
  - Cancel on the config download stops it, with no automatic retry.

Cancel on the progress popup itself: test_progress_popup_cancel.py.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import carveracontroller.main as main_module
from carveracontroller.Controller import CONN_USB
from carveracontroller.main import (
    CONFIG_DOWNLOAD_RETRY_DELAY_S,
    MAX_CONFIG_DOWNLOAD_ATTEMPTS,
    Makera,
)

CONFIG_TEXT = "multi_client.mode single_user\n"


class _Clock:
    """Kivy's Clock.schedule_once, run by hand: callbacks wait until run()
    and then go in order of their delay, then of scheduling."""

    def __init__(self):
        self.pending = []
        self._count = 0

    def schedule_once(self, callback, timeout=0):
        self._count += 1
        self.pending.append((timeout, self._count, callback))

    def run(self):
        while self.pending:
            self.pending.sort(key=lambda item: (item[0], item[1]))
            _timeout, _n, callback = self.pending.pop(0)
            callback(0)

    def has(self, callback):
        return any(cb is callback for _t, _n, cb in self.pending)


class _ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _write_config(tmp_filename, *_args):
    with open(tmp_filename, "w", encoding="utf-8") as handle:
        handle.write(CONFIG_TEXT)
    return len(CONFIG_TEXT)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A Makera connected over USB whose config.txt downloads return, in
    turn, the entries of ``host.results``: None (the machine ended the
    transfer), -1 (cancelled), a callable run as the transfer, or an
    exception to raise."""
    clock = _Clock()
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", clock.schedule_once)
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    app = SimpleNamespace(model="CA1", playing=False, state="Idle", selected_local_filename="")
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: app)

    results = []

    def download(tmp_filename, md5, progress_cb):
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result(tmp_filename) if callable(result) else result

    root = Makera.__new__(Makera)
    root.temp_dir = str(tmp_path)
    root.fw_version = "2.2.0c"
    root.heartbeat_time = 0
    root.filetype = ""
    root.config_loaded = False
    root.config_loading = False
    root._config_apply_failed = False
    root._config_download_failures = 0
    root._selected_file_machine_key = "usb:COM3"
    root.controller = SimpleNamespace(
        connection_type=CONN_USB,
        connection_address="COM3",
        comms=SimpleNamespace(uses_framed_transfer=True),
        downloadCommand=MagicMock(return_value=True),
        pauseStream=MagicMock(),
        resumeStream=MagicMock(),
        stream=SimpleNamespace(
            download=MagicMock(side_effect=download),
            modem=SimpleNamespace(last_file_error=None, download_md5_failed=False),
            cancel_process=MagicMock(),
        ),
        log=SimpleNamespace(put=MagicMock()),
        queryTime=MagicMock(),
        queryModel=MagicMock(),
        queryVersion=MagicMock(),
        queryFtype=MagicMock(),
        viewDiagnoseReport=MagicMock(),
    )
    for name in (
        "progressStart",
        "progressUpdate",
        "progressFinish",
        "updateStatus",
        "show_message_popup",
        "load_machine_config_defaults",
        "load_coordinates",
        "load_laser_offsets",
        "attempt_usb_baud_upgrade_if_eligible",
    ):
        setattr(root, name, MagicMock())
    root.load_machine_config = MagicMock(return_value=True)
    root.setting_list = {}
    root.config_popup = SimpleNamespace(btn_apply=SimpleNamespace(disabled=True))
    root.gcode_viewer = SimpleNamespace(high_precision_time_estimate=False)
    root.results = results
    root.clock = clock
    root.app = app
    return root


def _start_automatic_load(root):
    """What updateStatus does when _automatic_config_load_due is true."""
    root.config_loading = True
    root.download_config_file()


def _console(root):
    return [str(c.args[0][1]) for c in root.controller.log.put.call_args_list]


def _popups(root):
    return [str(c.args[0]) for c in root.show_message_popup.call_args_list]


# --- one config download at a time, retried only after a pause --------------


def test_a_failed_config_download_is_not_retried_straight_away(host):
    host.results.extend([None])
    _start_automatic_load(host)
    host.clock.run()

    now = main_module.time.time()
    assert host.config_loading is False
    assert host._config_download_failures == 1
    assert not host._automatic_config_load_due(host.app, now)
    assert host._automatic_config_load_due(host.app, now + CONFIG_DOWNLOAD_RETRY_DELAY_S + 0.1)


def test_retry_delay_outlasts_the_machines_pause_after_a_failed_transfer():
    # Player::download_command waits a second after ending a transfer.
    assert CONFIG_DOWNLOAD_RETRY_DELAY_S > 1.0


def test_no_automatic_load_while_a_config_download_is_running(host):
    host._config_download_running = True
    assert not host._automatic_config_load_due(host.app, main_module.time.time() + 3600)


def test_a_second_request_waits_for_the_running_download_and_then_runs(host):
    host.results.extend([None, _write_config])
    _start_automatic_load(host)  # the worker ran; finishLoadConfig is still pending
    assert host._config_download_running is True

    host.download_config_file()  # e.g. a settings Apply asking to read config.txt back
    assert host.controller.stream.download.call_count == 1

    host.clock.run()  # the first ends, then the second starts and ends

    assert host.controller.stream.download.call_count == 2
    assert host._config_download_running is False
    assert host.config_loaded is True


def test_after_the_last_attempt_the_popup_says_how_to_try_again(host):
    host._config_download_failures = MAX_CONFIG_DOWNLOAD_ATTEMPTS - 1
    host.results.extend([None])
    _start_automatic_load(host)
    host.clock.run()

    assert not host._automatic_config_load_due(host.app, main_module.time.time() + 3600)
    assert len(_popups(host)) == 1
    assert "Download config file error!" in _popups(host)[0]
    assert "Disconnect and connect again" in _popups(host)[0]


# --- a temporary file that cannot be deleted or renamed ---------------------


def _windows_lock(monkeypatch, *, replace=True, copy=True, remove=True):
    """Make the temporary file behave as if another handle held it open on
    Windows."""
    error = PermissionError(13, "The process cannot access the file because it is being used by another process")
    real_remove, real_replace, real_copy = os.remove, os.replace, main_module.shutil.copyfile

    def locked_remove(path, *a, **kw):
        if remove and str(path).endswith(".tmp"):
            raise error
        return real_remove(path, *a, **kw)

    def locked_replace(src, dst, *a, **kw):
        if replace and str(src).endswith(".tmp"):
            raise error
        return real_replace(src, dst, *a, **kw)

    def locked_copy(src, dst, *a, **kw):
        if copy and str(src).endswith(".tmp"):
            raise error
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(main_module.os, "remove", locked_remove)
    monkeypatch.setattr(main_module.os, "replace", locked_replace)
    monkeypatch.setattr(main_module.shutil, "copyfile", locked_copy)


def test_temp_file_that_cannot_be_deleted_after_a_failed_download_does_not_end_the_worker(host, monkeypatch):
    def machine_cancels(tmp_filename):
        # Part of the file arrived, then the machine ended the transfer (None).
        Path(tmp_filename).write_text("partial")

    host.results.extend([machine_cancels])
    _windows_lock(monkeypatch)

    _start_automatic_load(host)  # would raise PermissionError on the worker before
    assert host.clock.has(host.progressFinish)
    host.clock.run()

    assert host.config_loading is False
    assert host._config_download_running is False
    assert host._config_download_failures == 1
    assert any("Download config file error!" in text for text in _popups(host))


def test_finished_download_that_cannot_be_moved_or_copied_is_reported_and_retried(host, monkeypatch):
    host.results.extend([_write_config, _write_config])
    _windows_lock(monkeypatch)

    _start_automatic_load(host)  # would raise PermissionError on the worker before
    host.clock.run()

    assert host.config_loaded is False
    assert host.config_loading is False
    assert host._config_download_failures == 1
    assert any("being used by another process" in text for text in _popups(host))
    host.progressFinish.assert_called()

    # The file is free again: the next attempt loads the config.
    monkeypatch.undo()
    _host_clock_and_threads(host, monkeypatch)
    _start_automatic_load(host)
    host.clock.run()
    assert host.config_loaded is True
    assert Path(host._machine_config_cache_path()).read_text() == CONFIG_TEXT


def _host_clock_and_threads(host, monkeypatch):
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", host.clock.schedule_once)
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: host.app)


def test_finished_download_that_cannot_be_moved_is_copied_into_place(host, monkeypatch):
    host.results.extend([_write_config])
    _windows_lock(monkeypatch, copy=False)

    _start_automatic_load(host)
    host.clock.run()

    assert host.config_loaded is True
    assert Path(host._machine_config_cache_path()).read_text() == CONFIG_TEXT


def test_cached_copy_that_cannot_be_copied_aside_still_downloads(host, monkeypatch):
    """The unchanged-file check copies the cached config.txt to the
    temporary file first; when that copy fails the file is downloaded in
    full instead of the worker ending."""
    Path(host._machine_config_cache_path()).write_text("older\n")
    real_copy = main_module.shutil.copyfile

    def copy(src, dst, *a, **kw):
        if str(dst).endswith(".tmp"):
            raise PermissionError(13, "in use")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(main_module.shutil, "copyfile", copy)
    host.results.extend([_write_config])

    _start_automatic_load(host)
    host.clock.run()

    assert host.config_loaded is True
    assert Path(host._machine_config_cache_path()).read_text() == CONFIG_TEXT


# --- a worker that raises anyway ----------------------------------------------


def test_worker_that_raises_still_ends_the_download(host):
    host.doDownload = MagicMock(side_effect=RuntimeError("boom"))

    _start_automatic_load(host)
    host.clock.run()

    host.progressFinish.assert_called()
    assert host.config_loading is False
    assert host._config_download_running is False
    assert host._config_download_failures == 1
    assert any("boom" in text for text in _popups(host))


def test_worker_that_raises_after_its_download_ended_is_not_reported_twice(host):
    def ends_then_raises(*_args, **_kwargs):
        main_module.Clock.schedule_once(main_module.partial(host.finishLoadConfig, False), 0.1)
        raise RuntimeError("late")

    host.doDownload = ends_then_raises

    _start_automatic_load(host)
    host.clock.run()

    assert host._config_download_failures == 1


# --- Cancel -------------------------------------------------------------------


def test_cancelled_config_download_is_not_retried_on_this_connection(host):
    host.results.extend([-1])

    _start_automatic_load(host)
    host.clock.run()

    assert host.config_loading is False
    assert not host._automatic_config_load_due(host.app, main_module.time.time() + 3600)
    assert any("cancelled" in text for text in _console(host))
    assert not _popups(host)


def test_config_download_popup_offers_cancel(host):
    host.results.extend([_write_config])
    _start_automatic_load(host)
    host.clock.run()

    text, cancel_func = host.progressStart.call_args.args[:2]
    assert cancel_func == host.cancelProcessingFile
