"""Unit tests for Makera._auto_fetch_played_file / _finish_auto_fetch_played_file
/ _check_passive_fetch (carveracontroller/main.py): the glue between
PassiveFetchTracker's pure decision logic (machine/passive_fetch.py, covered
by tests/unit/test_passive_fetch.py) and the actual download + draw.

Bug this guards against: a passive controller (one without control) never
actually fetched the job file the machine told it about, and kept plotting
progress over whatever file happened to be open instead. The old code set
Kivy properties (selected_remote_filename/selected_local_filename) and
loaded the toolpath straight from the background thread that runs the
download, which raised "Cannot change graphics instruction outside the
main Kivy thread" and killed the thread before doDownload finished -- but
the `finally` still called mark_loaded(), so the tracker believed the file
was on screen and never retried. These tests cover the fix: the download
runs on the worker thread, every Kivy-touching step is deferred to the
main thread via Clock.schedule_once, and mark_loaded is only called once
that has actually happened.
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from carveracontroller.machine.passive_fetch import (
    MAX_FETCH_ATTEMPTS,
    RETRY_BACKOFF_S,
    PassiveFetchTracker,
)
from carveracontroller.main import Makera


class _MainThreadOnlyApp:
    """Stand-in for the real App: raises exactly like the file browser's
    breadcrumb redraw does (FileBrowserPopup._on_job_changed -> ... ->
    box.clear_widgets()) if selected_remote_filename / selected_local_filename
    are set from any thread other than the one recorded at construction."""

    def __init__(self, main_thread_id):
        self._main_thread_id = main_thread_id
        self.__dict__["selected_remote_filename"] = ""
        self.__dict__["selected_local_filename"] = ""

    def __setattr__(self, name, value):
        if name in ("selected_remote_filename", "selected_local_filename"):
            if threading.get_ident() != self._main_thread_id:
                raise RuntimeError("Cannot change graphics instruction outside the main Kivy thread")
        self.__dict__[name] = value


class _ImmediateThread:
    """Matches the pattern already used in tests/unit/test_file_browser.py:
    runs the target synchronously on .start() instead of spawning a real
    thread, so _check_passive_fetch's retry path can be driven step by step."""

    def __init__(self, target=None, args=(), **kwargs):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _passive_host(tmp_path):
    root = Makera.__new__(Makera)
    root.temp_dir = str(tmp_path)
    root._passive_fetch = PassiveFetchTracker()
    root._auto_fetch_in_progress = False
    return root


def test_mark_loaded_not_called_on_download_failure(monkeypatch, tmp_path):
    root = _passive_host(tmp_path)
    root._passive_fetch.note_published_file("/sd/job.nc")
    root._passive_fetch.mark_loaded = MagicMock(wraps=root._passive_fetch.mark_loaded)
    root._auto_fetch_in_progress = True
    root.doDownload = MagicMock(return_value=None)  # simulates the thread dying before a result
    root.load_gcode_file = MagicMock()
    scheduled = []
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: scheduled.append(cb))
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)

    Makera._auto_fetch_played_file(root, "/sd/job.nc")

    root._passive_fetch.mark_loaded.assert_not_called()
    root.load_gcode_file.assert_not_called()
    assert scheduled == []  # nothing was handed to the main thread -- the download never succeeded
    # Left pending, not marked loaded: a later status tick or publish event can retry.
    assert root._passive_fetch.pending_path == "/sd/job.nc"
    assert root._auto_fetch_in_progress is False


def test_mark_loaded_not_called_when_main_thread_load_raises(monkeypatch, tmp_path):
    """A download can succeed and still fail to draw (e.g. a corrupt file).
    mark_loaded must still only follow an actual successful load."""
    root = _passive_host(tmp_path)
    root.doDownload = MagicMock(return_value=1)
    root.load_gcode_file = MagicMock(side_effect=ValueError("bad gcode"))
    root._passive_fetch.mark_loaded = MagicMock(wraps=root._passive_fetch.mark_loaded)
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: cb())
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)

    Makera._auto_fetch_played_file(root, "/sd/job.nc")

    root._passive_fetch.mark_loaded.assert_not_called()
    assert root._passive_fetch.pending_path == "/sd/job.nc"
    assert root._auto_fetch_in_progress is False


def test_worker_thread_never_touches_kivy_properties_inline(monkeypatch, tmp_path):
    """The download (doDownload) and the UI/toolpath steps must be split
    across threads: the former on the worker thread, the latter scheduled
    for the main thread via Clock.schedule_once -- never run inline from
    the worker, which is exactly what used to crash."""
    root = _passive_host(tmp_path)
    root.doDownload = MagicMock(return_value=1)
    root.load_gcode_file = MagicMock()

    scheduled = []
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: scheduled.append(cb))

    main_thread_id = threading.get_ident()
    app = _MainThreadOnlyApp(main_thread_id)
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: app)

    errors = []

    def run_on_worker():
        try:
            Makera._auto_fetch_played_file(root, "/sd/job.nc")
        except Exception as exc:  # pragma: no cover - would mean the old bug is back
            errors.append(exc)

    worker = threading.Thread(target=run_on_worker)
    worker.start()
    worker.join(timeout=5)

    assert not errors, f"worker thread raised: {errors!r}"
    # Nothing Kivy-shaped happened inline on the worker thread.
    assert app.selected_remote_filename == ""
    assert app.selected_local_filename == ""
    root.load_gcode_file.assert_not_called()
    assert len(scheduled) == 1

    # The caller (Kivy's Clock) would run this on the main thread -- this
    # test thread, matching the id the fake app was built with.
    scheduled[0]()

    assert app.selected_remote_filename == "/sd/job.nc"
    assert app.selected_local_filename == os.path.join(str(tmp_path), "job.nc")
    root.load_gcode_file.assert_called_once_with(os.path.join(str(tmp_path), "job.nc"))
    assert root._auto_fetch_in_progress is False


def test_failed_fetch_does_not_retry_before_the_backoff_elapses(monkeypatch, tmp_path):
    """On failure the tracker is left pending so a later check retries the
    same path (per main.py's updateStatus()/on_passive_file_published()
    calling _check_passive_fetch again) -- but not immediately: a fixed
    retry backoff (machine/passive_fetch.py's RETRY_BACKOFF_S) keeps a
    persistently failing file from being retried on every status tick."""
    root = _passive_host(tmp_path)
    root._passive_fetch.note_published_file("/sd/job.nc")
    attempts = []
    clock = [0.0]

    def flaky_download(remote_path, local_path, show_progress=False, open_after=False, automatic=False):
        attempts.append(remote_path)
        return None if len(attempts) == 1 else 1

    root.doDownload = flaky_download
    root.load_gcode_file = MagicMock()
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: cb())
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("carveracontroller.main.time.monotonic", lambda: clock[0])

    # First check: due_fetch releases the path, the download fails.
    Makera._check_passive_fetch(root, is_idle=True)
    assert attempts == ["/sd/job.nc"]
    assert root.load_gcode_file.call_count == 0
    assert root._passive_fetch.pending_path == "/sd/job.nc"
    assert root._auto_fetch_in_progress is False

    # A status tick moments later must not retry yet -- still backing off.
    clock[0] += 1.0
    Makera._check_passive_fetch(root, is_idle=True)
    assert attempts == ["/sd/job.nc"]  # no second attempt
    assert root._passive_fetch.pending_path == "/sd/job.nc"  # still owed

    # Once the backoff has elapsed, the next check retries and succeeds.
    clock[0] += RETRY_BACKOFF_S
    Makera._check_passive_fetch(root, is_idle=True)
    assert attempts == ["/sd/job.nc", "/sd/job.nc"]
    root.load_gcode_file.assert_called_once()
    assert root._passive_fetch.pending_path is None
    assert root._auto_fetch_in_progress is False

    # And the tracker now genuinely considers it loaded -- a repeat
    # announcement of the same file is correctly deduplicated.
    root._passive_fetch.note_published_file("/sd/job.nc")
    assert root._passive_fetch.pending_path is None


def test_repeated_failures_eventually_stop_retrying_automatically(monkeypatch, tmp_path):
    root = _passive_host(tmp_path)
    root._passive_fetch.note_published_file("/sd/job.nc")
    attempts = []
    clock = [0.0]

    root.doDownload = MagicMock(side_effect=lambda *a, **kw: attempts.append(1) or None)
    root.load_gcode_file = MagicMock()
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: cb())
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("carveracontroller.main.time.monotonic", lambda: clock[0])

    for _ in range(MAX_FETCH_ATTEMPTS):
        Makera._check_passive_fetch(root, is_idle=True)
        clock[0] += RETRY_BACKOFF_S

    assert len(attempts) == MAX_FETCH_ATTEMPTS
    assert root._passive_fetch.pending_path is None
    # No further automatic attempts -- a status tick asks for nothing more.
    Makera._check_passive_fetch(root, is_idle=True)
    assert len(attempts) == MAX_FETCH_ATTEMPTS


def test_check_passive_fetch_does_not_overlap_a_fetch_already_in_flight(monkeypatch, tmp_path):
    root = _passive_host(tmp_path)
    root._passive_fetch.note_published_file("/sd/job.nc")
    root._auto_fetch_in_progress = True
    started = []
    monkeypatch.setattr(
        "carveracontroller.main.threading.Thread",
        lambda target=None, args=(), **kwargs: started.append((target, args)) or _ImmediateThread(target, args),
    )

    Makera._check_passive_fetch(root, is_idle=True)

    assert started == []  # never started a second worker thread
    # The path is still owed -- a fetch under way is not the same as loaded.
    assert root._passive_fetch.pending_path == "/sd/job.nc"


def test_on_passive_file_published_blanks_a_stale_selection(monkeypatch, tmp_path):
    """A passive controller had a different file open than the one the
    machine just started playing. The stale file's name and toolpath must
    not stay on screen under the new job's progress -- on_passive_file_published
    blanks the selection (the same state cancelSelectFile() uses), which
    gates updateStatus()'s progress/position plotting off until the fetch
    lands the right file."""
    root = _passive_host(tmp_path)
    root.controller = SimpleNamespace(has_control=False)
    root._check_passive_fetch = MagicMock()
    app = SimpleNamespace(
        selected_remote_filename="/sd/other.nc", selected_local_filename="/tmp/other.nc", state="Idle"
    )
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: app)

    Makera.on_passive_file_published(root, "/sd/job.nc")

    assert app.selected_remote_filename == ""
    assert app.selected_local_filename == ""
    assert root._passive_fetch.pending_path == "/sd/job.nc"
    root._check_passive_fetch.assert_called_once_with(True)


def test_on_passive_file_published_leaves_a_matching_selection_alone(monkeypatch, tmp_path):
    """If the announced file is already what is on screen (this controller
    fetched it itself last time around), there is nothing stale to blank."""
    root = _passive_host(tmp_path)
    root.controller = SimpleNamespace(has_control=False)
    root._check_passive_fetch = MagicMock()
    app = SimpleNamespace(selected_remote_filename="/sd/job.nc", selected_local_filename="/tmp/job.nc", state="Idle")
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: app)

    Makera.on_passive_file_published(root, "/sd/job.nc")

    assert app.selected_remote_filename == "/sd/job.nc"
    assert app.selected_local_filename == "/tmp/job.nc"


def test_on_passive_file_published_does_nothing_with_control(monkeypatch, tmp_path):
    root = _passive_host(tmp_path)
    root.controller = SimpleNamespace(has_control=True)
    root._check_passive_fetch = MagicMock()
    app = SimpleNamespace(
        selected_remote_filename="/sd/other.nc", selected_local_filename="/tmp/other.nc", state="Idle"
    )
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: app)

    Makera.on_passive_file_published(root, "/sd/job.nc")

    assert app.selected_remote_filename == "/sd/other.nc"  # untouched -- this controller has control
    root._check_passive_fetch.assert_not_called()
    assert root._passive_fetch.pending_path is None


def test_select_file_marks_the_tracker_loaded(monkeypatch, tmp_path):
    """A manual open (the file browser's "already cached" path) must
    update the passive-fetch tracker's memory of what is on screen -- see
    mark_loaded()'s own docstring ("the operator opened it manually").
    Otherwise a later play-started replay of a file this controller had
    already auto-fetched earlier, after the operator opened something
    else by hand, would be wrongly deduplicated against the stale
    memory."""
    root = _passive_host(tmp_path)
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: SimpleNamespace())
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda *a, **kw: None)

    Makera.select_file(root, "/sd/other.nc", "/tmp/other.nc")

    assert root._passive_fetch.pending_path is None
    # A later announcement of a *different*, previously auto-fetched file
    # is not dropped as already loaded -- "other.nc" is what is genuinely
    # on screen now.
    root._passive_fetch.note_published_file("/sd/job.nc")
    assert root._passive_fetch.pending_path == "/sd/job.nc"
