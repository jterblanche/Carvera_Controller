"""A file whose toolpath is too large for the memory free loads without
its toolpath, instead of the controller running out of memory drawing it.

Runs the real Makera.load_gcode_file on a worker thread with the test
thread playing Kivy's main thread, as test_file_load_main_thread does, and
records what reaches the viewer."""

from __future__ import annotations

import queue
import threading

import carveracontroller.main as main_module
from tests.unit.test_file_load_main_thread import _Guard, _load_host, _run_main_thread, _write_gcode

# Three batches and a bit (the loader hands the viewer a batch every
# LOAD_INTERVAL lines; load_constants sets it to 10000).
LINES = 3 * 10000 + 10


def _host(monkeypatch, budget):
    guard = _Guard()
    scheduled = queue.Queue()
    root = _load_host(monkeypatch, guard, scheduled)
    monkeypatch.setattr(main_module.toolpath_budget, "point_budget", lambda _available: budget)
    batches = []

    def load_array(points, is_end=True):
        batches.append((len(points), is_end))

    root.gcode_viewer.load_array = load_array
    root.gcode_viewer.begin_new_file_load = guard.wrap("viewer.begin_new_file_load")
    skipped = []
    root._show_toolpath_skipped = lambda value: skipped.append(value)
    return root, guard, scheduled, batches, skipped


def _load(root, guard, scheduled, path):
    worker = threading.Thread(target=root.load_gcode_file, args=(path,), daemon=True)
    worker.start()
    assert _run_main_thread(guard, scheduled, {"load_end", "load_error"})
    worker.join(timeout=5)
    assert "load_error" not in guard.names()


def test_toolpath_within_budget_is_drawn_in_full(monkeypatch, tmp_path):
    root, guard, scheduled, batches, _skipped = _host(monkeypatch, budget=10 * LINES)
    path = str(tmp_path / "job.nc")
    _write_gcode(path, LINES)

    _load(root, guard, scheduled, path)

    assert root._toolpath_skipped is None
    assert "viewer.begin_new_file_load" not in guard.names()
    assert main_module.LOAD_INTERVAL == 10000
    assert sum(count for count, _ in batches) > 2 * main_module.LOAD_INTERVAL
    assert batches[-1][1] is True


def test_toolpath_over_budget_is_dropped_and_the_file_still_loads(monkeypatch, tmp_path):
    path = str(tmp_path / "big.nc")
    _write_gcode(path, LINES)
    # Points in the first batch, from a load within the budget: a budget of
    # exactly that lets the first batch through and the second take it over.
    root, guard, scheduled, batches, _skipped = _host(monkeypatch, budget=10 * LINES)
    _load(root, guard, scheduled, path)
    budget = batches[0][0]

    root, guard, scheduled, batches, _skipped = _host(monkeypatch, budget=budget)
    _load(root, guard, scheduled, path)

    # The first batch was within the budget and went to the viewer; the
    # second took it over: what was drawn is dropped, on the main thread,
    # and nothing more is sent, but the load still ends normally.
    points, seen_budget = root._toolpath_skipped
    assert points > budget and seen_budget == budget
    assert batches[0][0] > 0
    assert all(count == 0 for count, _ in batches[1:])
    assert batches[-1] == (0, True)
    assert "viewer.begin_new_file_load" in guard.names()
    assert guard.off_main() == []
    # The text of the whole file is still there, for the G-code pane and the run.
    assert root.selected_file_line_count == LINES


def test_next_load_starts_with_a_fresh_decision(monkeypatch, tmp_path):
    root, guard, scheduled, _batches, _skipped = _host(monkeypatch, budget=10)
    big = str(tmp_path / "big.nc")
    _write_gcode(big, LINES)
    _load(root, guard, scheduled, big)
    assert root._toolpath_skipped is not None

    monkeypatch.setattr(main_module.toolpath_budget, "point_budget", lambda _available: 10 * LINES)
    guard.calls.clear()
    small = str(tmp_path / "small.nc")
    _write_gcode(small, 50)
    _load(root, guard, scheduled, small)
    assert root._toolpath_skipped is None


def test_load_end_tells_the_user_and_turns_off_toolpath_features(monkeypatch):
    shown = []
    logged = []
    root = main_module.Makera.__new__(main_module.Makera)
    root.show_message_popup = lambda message, disabled: shown.append(message)
    root.controller = type("C", (), {"log": type("L", (), {"put": staticmethod(logged.append)})()})()
    monkeypatch.setattr(
        "carveracontroller.main.App.get_running_app", lambda: type("A", (), {"selected_local_filename": "big.nc"})()
    )

    root._show_toolpath_skipped((2_000_001, 2_000_000))

    assert root.gcode_cannot_visualise is True
    assert len(shown) == 1 and "too large to draw" in shown[0] and "can still be run" in shown[0]
    assert logged and logged[0][1] == shown[0]
