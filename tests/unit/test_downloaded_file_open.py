"""Opening a file the file browser has just downloaded from the machine
(Makera._finish_downloaded_file_open, scheduled by doDownload's open_after
branch).

load_gcode_file parses the file and, after every LOAD_INTERVAL lines, waits
on load_event until the main thread has run the batch it scheduled with
Clock.schedule_once (load_gcodes sets the event again). Run on the main
thread itself, it waits for work that only it can do, so any file longer
than LOAD_INTERVAL lines stops the controller for good. These tests drive
the real load_gcode_file with a stand-in Kivy clock: a thread that plays the
main thread and runs the scheduled callbacks in order.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import carveracontroller.main as main_module
from carveracontroller.CNC import CNC
from carveracontroller.main import Makera

# Long enough for any loaded file to show the fault; a hung load is reported
# as a failure after this rather than stopping the test run.
LOAD_TIMEOUT_S = 30


class _MainThread:
    """Plays Kivy's main thread: runs `first` and then every callback handed
    to Clock.schedule_once, in order, until load_end has run or the test
    gives up."""

    def __init__(self):
        self.queue = queue.Queue()
        self.ident = None
        self.finished = threading.Event()
        self.stop = threading.Event()

    def schedule_once(self, callback, *_args, **_kwargs):
        self.queue.put(callback)

    def run(self, first):
        self.ident = threading.get_ident()
        first()
        while not self.stop.is_set() and not self.finished.is_set():
            try:
                callback = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            callback(0)


def _write_gcode(path, line_count):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("G21\nG90\n")
        for i in range(line_count - 2):
            handle.write("G1 X%.3f Y%.3f F1000\n" % (i % 100, (i // 100) % 100))


def _open_host(monkeypatch, main_thread):
    main_module.load_constants()
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", main_thread.schedule_once)
    monkeypatch.setattr(
        "carveracontroller.main.App.get_running_app",
        lambda: SimpleNamespace(total_pages=0, curr_page=1),
    )

    root = Makera.__new__(Makera)
    root.load_event = threading.Event()
    root.load_canceled = False
    root.cnc = CNC()
    root.gcode_viewer = SimpleNamespace(tool_table={}, tool_unit_scale=1.0, load_array=MagicMock())
    root.progress_popup = SimpleNamespace(
        btn_cancel=SimpleNamespace(disabled=True), progress_text="", progress_value=0, cancel=None
    )
    root.controller = SimpleNamespace(log=SimpleNamespace(put=MagicMock()), stream=None)
    root.reset_stock_for_loaded_file = MagicMock()
    root.load_start = MagicMock()
    root.load_page = MagicMock()
    root.load_error = MagicMock()
    root.load_end = MagicMock(side_effect=lambda *_: main_thread.finished.set())
    root._ingest_machine_gcode_thumbnail = MagicMock()
    return root


def _open_downloaded(root, main_thread, remote_path, local_path):
    """Run _finish_downloaded_file_open on the stand-in main thread, as
    doDownload schedules it. Returns True if loading ran to load_end in
    time; on a hang, frees the stuck load before returning False."""
    runner = threading.Thread(
        target=main_thread.run,
        args=(lambda: Makera._finish_downloaded_file_open(root, remote_path, local_path),),
        daemon=True,
    )
    runner.start()
    done = main_thread.finished.wait(LOAD_TIMEOUT_S)
    if not done:
        root.load_canceled = True
        root.load_event.set()
    main_thread.stop.set()
    runner.join(timeout=5)
    return done


@pytest.mark.parametrize("extra_lines", [1, 15000])
def test_downloaded_file_longer_than_one_load_interval_opens(monkeypatch, tmp_path, extra_lines):
    main_thread = _MainThread()
    root = _open_host(monkeypatch, main_thread)
    line_count = main_module.LOAD_INTERVAL + extra_lines
    local_path = str(tmp_path / "long_job.nc")
    _write_gcode(local_path, line_count)

    loaded = _open_downloaded(root, main_thread, "/sd/gcodes/long_job.nc", local_path)

    assert loaded, "opening a downloaded file of %d lines did not finish within %ss" % (line_count, LOAD_TIMEOUT_S)
    assert root.selected_file_line_count == line_count
    batches = root.gcode_viewer.load_array.call_args_list
    assert len(batches) == -(-line_count // main_module.LOAD_INTERVAL)  # one per interval, plus the remainder
    assert batches[-1].args[1] is True  # the last batch is marked as the end
    root.load_error.assert_not_called()


def test_downloaded_file_is_parsed_off_the_main_thread_then_its_thumbnail_ingested(monkeypatch, tmp_path):
    main_thread = _MainThread()
    root = _open_host(monkeypatch, main_thread)
    local_path = str(tmp_path / "job.nc")
    _write_gcode(local_path, 50)

    parsed_on = []
    real_load = Makera.load_gcode_file

    def load(path):
        parsed_on.append(threading.get_ident())
        real_load(root, path)

    root.load_gcode_file = load
    thumbnail_done = threading.Event()
    root._ingest_machine_gcode_thumbnail = MagicMock(side_effect=lambda *_: thumbnail_done.set())

    assert _open_downloaded(root, main_thread, "/sd/gcodes/job.nc", local_path)
    # The thumbnail is ingested after the load (which decompresses a QuickLZ
    # file in place first), on the loading thread.
    assert thumbnail_done.wait(5)

    assert parsed_on and parsed_on[0] != main_thread.ident
    root._ingest_machine_gcode_thumbnail.assert_called_once_with("/sd/gcodes/job.nc", local_path)
