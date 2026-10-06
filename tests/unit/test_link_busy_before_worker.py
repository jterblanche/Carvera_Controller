"""A listing, open-from-machine or save-to-computer marks the link busy on
the main thread, before its worker thread starts.

Each of these starts a worker thread that marks the link busy itself a
moment later (``loadNUM`` for a listing, ``downloading`` for a download).
A status update handled on the main thread in between could otherwise find
the link free and start a passive fetch, which then collides with the
listing or download on the link. The mark set on the main thread lasts
until the worker returns, however it ends.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller.machine.job_start import JobStartTracker
from carveracontroller.machine.local_copies import LocalCopyStore
from carveracontroller.machine.passive_fetch import PassiveFetchTracker
from carveracontroller.main import Makera

THEIRS = "/sd/gcodes/theirs.nc"


class _DeferredThread:
    """Records each started thread; the test runs it when it chooses, the
    way the scheduler may let the main thread run first."""

    started: list[_DeferredThread] = []

    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.ran = False

    def start(self):
        _DeferredThread.started.append(self)

    def run_now(self):
        self.ran = True
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def threads(monkeypatch):
    _DeferredThread.started = []
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _DeferredThread)
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda *a, **kw: None)
    running = SimpleNamespace(selected_remote_filename="", selected_local_filename="", state="Idle")
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: running)
    return _DeferredThread.started


def _host(tmp_path):
    root = Makera.__new__(Makera)
    root.temp_dir = str(tmp_path / "cache")
    root._passive_fetch = PassiveFetchTracker()
    root._local_copies = LocalCopyStore()
    root._job_start = JobStartTracker(own_id=1)
    root._machine_ls_lock = threading.Lock()
    root._machine_ls_wanted_path = None
    root._machine_ls_sent_path = None
    root.file_popup = SimpleNamespace(
        machine_dir="/sd/gcodes",
        selected_machine_file="/sd/gcodes/mine.nc",
        selected_machine_filesize=10,
    )
    root.wpb_play = SimpleNamespace(value=0)
    root.controller = SimpleNamespace(
        has_control=False,
        sendNUM=0,
        loadNUM=0,
        load_buffer=queue.Queue(),
        log=queue.Queue(),
        lsCommand=MagicMock(),
    )
    root.downloads = []

    def download(remote_path, local_path, show_progress=True, open_after=True, automatic=False):
        root.downloads.append((remote_path, automatic))
        return 1

    root.doDownload = download
    root.progressStart = MagicMock()
    return root


def _list(root, tmp_path):
    Makera.request_machine_ls(root, "/sd/gcodes")


def _open_from_machine(root, tmp_path):
    Makera.check_and_download(root)


def _save_to_computer(root, tmp_path):
    Makera.save_machine_file_to_device(root, "/sd/gcodes/mine.nc", str(tmp_path / "saved.nc"))


ACTIONS = {"listing": _list, "open-from-machine": _open_from_machine, "save-to-computer": _save_to_computer}


def _fetch_owed(root):
    root._passive_fetch.note_published_file(THEIRS)


@pytest.mark.parametrize("name", list(ACTIONS))
def test_status_update_before_the_worker_runs_does_not_start_a_fetch(tmp_path, threads, name):
    root = _host(tmp_path)
    _fetch_owed(root)

    ACTIONS[name](root, tmp_path)
    assert len(threads) == 1  # the worker has been started but has not run yet

    # A status update reaches the main thread before the worker runs.
    Makera._check_passive_fetch(root, is_idle=True)

    assert root._auto_fetch_in_progress is False
    assert len(threads) == 1
    assert root._passive_fetch.pending_path == THEIRS


@pytest.mark.parametrize("name", list(ACTIONS))
def test_fetch_starts_once_the_worker_has_finished_with_the_link(tmp_path, threads, name):
    root = _host(tmp_path)
    _fetch_owed(root)

    ACTIONS[name](root, tmp_path)
    threads[0].run_now()
    # A listing keeps the link until its reply has ended.
    root.controller.loadNUM = 0

    Makera._check_passive_fetch(root, is_idle=True)

    assert root._auto_fetch_in_progress is True
    assert len(threads) == 2


def test_mark_is_cleared_when_the_worker_raises(tmp_path, threads):
    root = _host(tmp_path)

    def broken_download(*args, **kwargs):
        raise OSError("disk full")

    root.doDownload = broken_download
    _save_to_computer(root, tmp_path)

    with pytest.raises(OSError):
        threads[0].run_now()

    assert Makera._link_busy_for_passive_fetch(root) is False


def test_mark_is_cleared_when_the_worker_cannot_start(tmp_path, threads, monkeypatch):
    class _CannotStart(_DeferredThread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr("carveracontroller.main.threading.Thread", _CannotStart)
    root = _host(tmp_path)

    with pytest.raises(RuntimeError):
        _save_to_computer(root, tmp_path)

    assert Makera._link_busy_for_passive_fetch(root) is False


def test_listing_already_running_starts_no_worker_and_leaves_no_mark(tmp_path, threads):
    """A second listing request while one is running only records the new
    folder; no worker starts, so nothing is marked."""
    root = _host(tmp_path)
    _list(root, tmp_path)
    threads[0].run_now()

    _list(root, tmp_path)

    assert len(threads) == 1
    root.controller.loadNUM = 0
    assert Makera._link_busy_for_passive_fetch(root) is False
