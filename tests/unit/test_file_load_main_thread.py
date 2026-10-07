"""Makera.load_gcode_file runs on a worker thread when a file is opened (the
file browser, select after upload, the facing wizard), so every step of a
load that touches widgets, UI-bound Kivy properties or GL state must be
handed to the main thread with Clock.schedule_once.

These tests run the real load_gcode_file on a worker thread, with the test
thread playing Kivy's main thread: it runs the callbacks handed to
Clock.schedule_once, in order. Each main-thread-only step is guarded and
records the thread it ran on.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import carveracontroller.main as main_module
from carveracontroller.CNC import CNC
from carveracontroller.main import Makera

LOAD_TIMEOUT_S = 30


class _Guard:
    """Records each guarded call and whether it ran on the main thread."""

    def __init__(self):
        self.main_ident = threading.get_ident()
        self.calls = []

    def wrap(self, name, result=None):
        def guarded(*_args, **_kwargs):
            self.calls.append((name, threading.get_ident() == self.main_ident))
            return result

        return guarded

    def off_main(self):
        return [name for name, on_main in self.calls if not on_main]

    def names(self):
        return [name for name, _ in self.calls]


def _write_gcode(path, line_count):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("G21\nG90\n")
        for i in range(line_count - 2):
            handle.write("G1 X%.3f Y%.3f F1000\n" % (i % 100, (i // 100) % 100))


def _load_host(monkeypatch, guard, scheduled):
    main_module.load_constants()
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: scheduled.put(cb))
    monkeypatch.setattr(
        "carveracontroller.main.App.get_running_app",
        lambda: SimpleNamespace(total_pages=0, curr_page=1),
    )

    root = Makera.__new__(Makera)
    root.load_event = threading.Event()
    root.load_canceled = False
    root.cnc = CNC()
    root.gcode_viewer = SimpleNamespace(
        tool_table={},
        tool_unit_scale=1.0,
        simulation_available=lambda: False,
        set_stock=guard.wrap("viewer.set_stock"),
        load_array=guard.wrap("viewer.load_array"),
    )
    root.stock_settings_popup = SimpleNamespace(
        reset_for_loaded_file=guard.wrap("popup.reset_for_loaded_file", main_module.default_settings())
    )
    root.progress_popup = SimpleNamespace(
        btn_cancel=SimpleNamespace(disabled=True), progress_text="", progress_value=0, cancel=None
    )
    root.controller = SimpleNamespace(log=SimpleNamespace(put=MagicMock()), stream=None)
    root.load_start = guard.wrap("load_start")
    root.load_page = guard.wrap("load_page")
    root.load_end = guard.wrap("load_end")
    root.load_error = guard.wrap("load_error")
    # The Resume at line button and checkbox are bound to this property.
    root.bind(gcode_cannot_visualise=guard.wrap("gcode_cannot_visualise changed"))
    return root


def _run_main_thread(guard, scheduled, until):
    """Run scheduled callbacks on this (the main) thread until one of the
    names in `until` has been called, or time out."""
    deadline = threading.Event()
    timer = threading.Timer(LOAD_TIMEOUT_S, deadline.set)
    timer.start()
    try:
        while not deadline.is_set():
            if any(name in until for name in guard.names()):
                return True
            try:
                callback = scheduled.get(timeout=0.05)
            except queue.Empty:
                continue
            callback(0)
        return False
    finally:
        timer.cancel()


def _load_on_worker(root, path):
    worker = threading.Thread(target=root.load_gcode_file, args=(path,), daemon=True)
    worker.start()
    return worker


def test_load_on_a_worker_does_widget_and_gl_work_only_on_the_main_thread(monkeypatch, tmp_path):
    guard = _Guard()
    scheduled = queue.Queue()
    root = _load_host(monkeypatch, guard, scheduled)
    path = str(tmp_path / "job.nc")
    _write_gcode(path, main_module.LOAD_INTERVAL + 500)

    worker = _load_on_worker(root, path)
    assert _run_main_thread(guard, scheduled, {"load_end", "load_error"})
    worker.join(timeout=5)

    # reset_stock_for_loaded_file is @mainthread, which schedules it through
    # the same Clock, so the stock popup and viewer are only touched there.
    assert guard.off_main() == []
    names = guard.names()
    assert "load_error" not in names
    # The previous stock is hidden before load_end sets up the new file's.
    assert names.index("popup.reset_for_loaded_file") < names.index("load_end")
    assert names.index("viewer.set_stock") < names.index("load_end")


def test_failed_load_on_a_worker_flags_it_only_on_the_main_thread(monkeypatch, tmp_path):
    guard = _Guard()
    scheduled = queue.Queue()
    root = _load_host(monkeypatch, guard, scheduled)
    root.cnc.parseLine = MagicMock(side_effect=ValueError("bad line"))
    path = str(tmp_path / "bad.nc")
    _write_gcode(path, 20)

    worker = _load_on_worker(root, path)
    assert _run_main_thread(guard, scheduled, {"load_error"})
    worker.join(timeout=5)

    assert root.gcode_cannot_visualise is True
    assert "gcode_cannot_visualise changed" in guard.names()
    assert guard.off_main() == []


def test_load_called_on_the_main_thread_still_resets_stock_before_load_end(monkeypatch, tmp_path):
    guard = _Guard()
    scheduled = queue.Queue()
    root = _load_host(monkeypatch, guard, scheduled)
    path = str(tmp_path / "short.nc")
    _write_gcode(path, 50)

    root.load_gcode_file(path)
    assert _run_main_thread(guard, scheduled, {"load_end", "load_error"})

    assert guard.off_main() == []
    names = guard.names()
    assert names.index("viewer.set_stock") < names.index("load_end")


def _drain(scheduled):
    """Run every queued callback, including any they queue, on this thread."""
    while True:
        try:
            callback = scheduled.get_nowait()
        except queue.Empty:
            return
        callback(0)


def _fail_fast_host(monkeypatch, guard, scheduled):
    root = _load_host(monkeypatch, guard, scheduled)
    root._mark_gcode_cannot_visualise = guard.wrap("_mark_gcode_cannot_visualise")

    def load_start(*_args):
        guard.calls.append(("load_start", threading.get_ident() == guard.main_ident))
        root.loading_file = True

    root.load_start = load_start
    root.loading_file = False
    return root


def _fail_before_main_thread_runs(root, scheduled, path):
    """The worker finishes (fails) before the main thread runs anything it
    scheduled, then the main thread catches up."""
    worker = _load_on_worker(root, path)
    worker.join(timeout=LOAD_TIMEOUT_S)
    assert not worker.is_alive()
    _drain(scheduled)


def test_load_that_fails_at_once_does_not_leave_loading_file_set(monkeypatch, tmp_path):
    guard = _Guard()
    scheduled = queue.Queue()
    root = _fail_fast_host(monkeypatch, guard, scheduled)

    _fail_before_main_thread_runs(root, scheduled, str(tmp_path / "missing.nc"))

    assert "load_error" in guard.names()
    assert root.loading_file is False


def test_load_whose_decompress_fails_does_not_leave_loading_file_set(monkeypatch, tmp_path):
    guard = _Guard()
    scheduled = queue.Queue()
    root = _fail_fast_host(monkeypatch, guard, scheduled)
    path = tmp_path / "broken.nc"
    path.write_bytes(b"\x00\x00not a quicklz stream")  # the QuickLZ magic, then nothing valid

    _fail_before_main_thread_runs(root, scheduled, str(path))

    assert "load_start" in guard.names()
    assert root.loading_file is False


def test_failed_reload_of_the_loaded_file_does_not_offer_resume_from_old_lines(monkeypatch, tmp_path):
    """The file was loaded once; loading it again fails at the decompress, so
    self.lines still holds the earlier copy. Resume at line must not be
    offered against those lines."""
    guard = _Guard()
    scheduled = queue.Queue()
    root = _fail_fast_host(monkeypatch, guard, scheduled)
    path = tmp_path / "job.nc"
    app = SimpleNamespace(
        total_pages=0, curr_page=1, selected_remote_filename="/sd/gcodes/job.nc", selected_local_filename=str(path)
    )
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: app)
    root.lines = ["G21\n", "G1 X1 F100\n"]
    root.selected_file_line_count = 2
    root._last_loaded_file_key = "/sd/gcodes/job.nc"
    path.write_bytes(b"\x00\x00not a quicklz stream")

    _fail_before_main_thread_runs(root, scheduled, str(path))

    assert root.loading_file is False
    assert root._resume_gcode_lines_available() is False
