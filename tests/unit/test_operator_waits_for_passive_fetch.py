"""An operator action that needs the link waits for a passive fetch in
progress, then runs.

The machine runs one file transfer or load command at a time: a listing,
upload, delete or any other load command sent while a download is running
ends both. A passive fetch (another controller's file, downloaded
automatically, see Makera._check_passive_fetch) can be running when the
operator asks for one of these. The action must then wait until the fetch
has finished, with a hint on screen, and run straight after it. The fetch
itself must finish as normal, and no error popup may appear.

These start a passive fetch exactly as a published event does
(Makera.on_passive_file_published), and have the operator act while its
download is still running: the stand-in doDownload calls the operator's
action from inside the download, the way a click on the main thread
arrives while the fetch's worker thread is downloading.
"""

from __future__ import annotations

import hashlib
import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller.Controller import LOAD_DIR, SEND_FILE
from carveracontroller.machine.job_start import JobStartTracker
from carveracontroller.machine.local_copies import LocalCopyStore
from carveracontroller.machine.passive_fetch import PassiveFetchTracker
from carveracontroller.main import Makera

THEIRS = "/sd/gcodes/theirs.nc"
WAIT_HINT = "Waiting for a download to finish"


class _ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def clock(monkeypatch):
    """Clock.schedule_once callbacks, kept so a test can run the main
    thread's next frame when it chooses (run_clock)."""
    scheduled = []
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: scheduled.append(cb))
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    return scheduled


@pytest.fixture
def app(monkeypatch):
    running = SimpleNamespace(
        selected_remote_filename="/sd/gcodes/other.nc",
        selected_local_filename="/tmp/other.nc",
        state="Idle",
    )
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: running)
    return running


def run_clock(scheduled):
    """Run every callback scheduled so far, and any they schedule in turn."""
    while scheduled:
        scheduled.pop(0)(0)


class _Host:
    """A Makera with a stand-in link that records, in order, every
    transfer and load command sent over it."""

    def __init__(self, tmp_path):
        self.events: list[str] = []
        self.during_fetch = None
        self.fetch_result = 1
        root = Makera.__new__(Makera)
        root.temp_dir = str(tmp_path / "cache")
        root._passive_fetch = PassiveFetchTracker()
        root._local_copies = LocalCopyStore()
        root._job_start = JobStartTracker(own_id=1)
        root._auto_fetch_in_progress = False
        root._uploading_firmware = False
        root.filetype = ""
        root._machine_ls_lock = threading.Lock()
        root._machine_ls_wanted_path = None
        root._machine_ls_sent_path = None
        root.file_popup = SimpleNamespace(
            machine_dir="/sd/gcodes",
            firmware_mode=False,
            selected_machine_file="/sd/gcodes/mine.nc",
            selected_machine_filesize=10,
            refresh_machine=MagicMock(),
        )
        root.input_popup = SimpleNamespace(txt_content=SimpleNamespace(text=""), cache_var1="")
        root.wpb_play = SimpleNamespace(value=0)
        root.wifi_ap_drop_down = MagicMock()
        root.wifi_ap_status_bar = MagicMock()
        root.controller = SimpleNamespace(
            has_control=False,
            sendNUM=0,
            loadNUM=0,
            load_buffer=queue.Queue(),
            log=queue.Queue(),
            lsCommand=self._record("ls"),
            rmCommand=self._record("rm"),
            mvCommand=self._record("mv"),
            mkdirCommand=self._record("mkdir"),
            loadWiFiCommand=self._record("wifi-list"),
            connectWiFiCommand=self._record("wifi-connect"),
        )
        root.doDownload = self._download
        root.doUpload = self._record("upload")
        root.load_gcode_file = MagicMock()
        root.progressStart = MagicMock()
        root.progressFinish = MagicMock()
        root.show_message_popup = MagicMock()
        root.loadError = MagicMock()
        self.root = root

    def _record(self, name):
        def record(*args, **kwargs):
            self.events.append(name)
            return True

        return record

    def _download(self, remote_path, local_path, show_progress=True, open_after=True, automatic=False):
        if not automatic:
            self.events.append("download")
            return 1
        self.events.append("fetch")
        self.root.downloading = True
        if self.during_fetch is not None:
            action, self.during_fetch = self.during_fetch, None
            action()
        self.root.downloading = False
        self.events.append("fetch done")
        return self.fetch_result

    def publish(self, path=THEIRS, content=b"theirs"):
        Makera.on_passive_file_published(self.root, path, hashlib.md5(content).digest())

    def error_popups(self):
        return [c.args[0] for c in self.root.show_message_popup.call_args_list if "rror" in str(c.args[0])]

    def hint_shown(self):
        return any(WAIT_HINT in str(call.args[0]) for call in self.root.progressStart.call_args_list)


@pytest.fixture
def host(tmp_path, app, clock):
    return _Host(tmp_path)


def _local_job(tmp_path):
    path = tmp_path / "mine.nc"
    path.write_bytes(b"G0 X1\n")
    return str(path)


def _list(host, tmp_path):
    Makera.request_machine_ls(host.root, "/sd/gcodes")


def _upload(host, tmp_path):
    Makera.uploadLocalFile(host.root, _local_job(tmp_path))


def _delete(host, tmp_path):
    Makera.removeRemoteFile(host.root, "/sd/gcodes/mine.nc")


def _rename(host, tmp_path):
    host.root.input_popup.txt_content.text = "renamed.nc"
    assert Makera.renameRemoteFile(host.root, "/sd/gcodes/mine.nc") is True


def _make_dir(host, tmp_path):
    host.root.input_popup.txt_content.text = "jobs"
    assert Makera.createRemoteDir(host.root) is True


def _open_from_machine(host, tmp_path):
    Makera.check_and_download(host.root)


def _save_to_computer(host, tmp_path):
    Makera.save_machine_file_to_device(host.root, "/sd/gcodes/mine.nc", str(tmp_path / "saved.nc"))


def _wifi_list(host, tmp_path):
    Makera.startLoadWiFi(host.root, MagicMock())


def _wifi_connect(host, tmp_path):
    host.root.input_popup.cache_var1 = "workshop"
    host.root.input_popup.txt_content.text = "secret"
    assert Makera.connectToWiFi(host.root) is True


OPERATOR_ACTIONS = {
    "listing": (_list, "ls"),
    "upload": (_upload, "upload"),
    "delete": (_delete, "rm"),
    "rename": (_rename, "mv"),
    "make-dir": (_make_dir, "mkdir"),
    "open-from-machine": (_open_from_machine, "download"),
    "save-to-computer": (_save_to_computer, "download"),
    "wifi-list": (_wifi_list, "wifi-list"),
    "wifi-connect": (_wifi_connect, "wifi-connect"),
}


@pytest.mark.parametrize("name", list(OPERATOR_ACTIONS))
def test_action_during_a_fetch_runs_once_the_fetch_has_finished(host, tmp_path, clock, app, name):
    act, command = OPERATOR_ACTIONS[name]
    host.during_fetch = lambda: act(host, tmp_path)

    host.publish()
    # The fetch's download has returned; its result is drawn on the main
    # thread next, and the held action runs after that.
    assert host.events == ["fetch", "fetch done"]
    assert host.hint_shown()

    run_clock(clock)

    assert host.events == ["fetch", "fetch done", command]
    # The fetch was not lost: its file was drawn and is no longer owed.
    host.root.load_gcode_file.assert_called_once()
    assert host.root.load_gcode_file.call_args.args[0].endswith("theirs.nc")
    assert host.root._passive_fetch.pending_path is None
    host.root.progressFinish.assert_called()
    assert not host.error_popups()
    host.root.loadError.assert_not_called()


@pytest.mark.parametrize("name", list(OPERATOR_ACTIONS))
def test_action_with_no_fetch_running_goes_out_at_once(host, tmp_path, clock, name):
    act, command = OPERATOR_ACTIONS[name]

    act(host, tmp_path)

    assert host.events == [command]
    assert not host.hint_shown()


def test_opening_a_file_during_a_fetch_leaves_the_operators_file_selected(host, tmp_path, clock, app):
    """The fetched file is drawn first; the file the operator chose to open
    is selected after it, since that was asked for last."""
    host.during_fetch = lambda: _open_from_machine(host, tmp_path)

    host.publish()
    run_clock(clock)

    assert app.selected_remote_filename == "/sd/gcodes/mine.nc"


def test_rename_keeps_the_name_typed_before_the_wait(host, tmp_path, clock):
    """The new name is read when the operator confirms it, not when the
    held rename finally runs, by which time the input box may hold
    something else."""
    calls = []
    host.root.controller.mvCommand = lambda old, new: calls.append((old, new)) or True
    host.during_fetch = lambda: _rename(host, tmp_path)

    host.publish()
    host.root.input_popup.txt_content.text = "something else"
    run_clock(clock)

    assert calls == [("/sd/gcodes/mine.nc", "/sd/gcodes/renamed.nc")]


def test_a_failed_fetch_still_releases_the_action_and_stays_owed(host, tmp_path, clock):
    host.fetch_result = None
    host.during_fetch = lambda: _upload(host, tmp_path)

    host.publish()
    run_clock(clock)

    assert host.events == ["fetch", "fetch done", "upload"]
    # Owed still, for a later retry once its back-off has passed.
    assert host.root._passive_fetch.pending_path == THEIRS
    host.root.load_gcode_file.assert_not_called()


def test_no_new_fetch_starts_before_the_held_action_has_claimed_the_link(host, tmp_path, clock):
    """Between the fetch handing the link back and the held action running
    on the next frame, another controller's upload is announced. Its fetch
    must not take the link first: it waits for the held listing, then for
    the listing to finish, and then runs."""
    host.during_fetch = lambda: _list(host, tmp_path)

    host.publish()
    clock.pop(0)(0)  # the first fetch's result is drawn; the listing is released
    host.publish("/sd/gcodes/second.nc", b"second")
    assert host.events == ["fetch", "fetch done"]

    run_clock(clock)
    assert host.events == ["fetch", "fetch done", "ls"]
    assert host.root.controller.loadNUM == LOAD_DIR
    assert host.root._passive_fetch.pending_path == "/sd/gcodes/second.nc"

    host.root.controller.loadNUM = 0  # the listing has finished
    Makera._check_passive_fetch(host.root, is_idle=True)
    assert host.events == ["fetch", "fetch done", "ls", "fetch", "fetch done"]


def test_cancelling_the_wait_drops_only_the_held_action(host, tmp_path, clock):
    """The hint can be cancelled. That drops the held action, says so in
    the console, and leaves the fetch to finish as normal."""
    host.during_fetch = lambda: _upload(host, tmp_path)

    host.publish()
    hint = next(c for c in host.root.progressStart.call_args_list if WAIT_HINT in str(c.args[0]))
    cancel = hint.args[1]
    cancel()
    run_clock(clock)

    assert host.events == ["fetch", "fetch done"]
    assert host.root.controller.sendNUM == 0
    host.root.load_gcode_file.assert_called_once()
    assert host.root._passive_fetch.pending_path is None
    logged = []
    while not host.root.controller.log.empty():
        logged.append(host.root.controller.log.get_nowait()[1])
    assert any("not started" in line for line in logged)


def test_upload_waiting_for_a_fetch_does_not_claim_the_link_yet(host, tmp_path, clock):
    """A held upload marks nothing as its own until it runs: sendNUM stays
    clear while it waits, so status queries carry on."""
    seen = []

    def act():
        _upload(host, tmp_path)
        seen.append(host.root.controller.sendNUM)

    host.during_fetch = act
    host.publish()

    assert seen == [0]
    run_clock(clock)
    assert host.root.controller.sendNUM == SEND_FILE


def test_fetch_does_not_start_during_a_config_backup(host, clock):
    """A config backup lists /sd and then downloads several files one after
    another; a fetch must not slip into the gap between two of them."""
    host.root.backing_up_config = True

    host.publish()
    assert host.events == []
    assert host.root._passive_fetch.pending_path == THEIRS

    host.root.backing_up_config = False
    Makera._check_passive_fetch(host.root, is_idle=True)
    assert host.events == ["fetch", "fetch done"]
