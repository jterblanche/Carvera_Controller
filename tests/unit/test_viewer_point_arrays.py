"""How the viewer stores a loaded toolpath: per-point data in typed arrays,
meshes drawn from views into them, and nothing of an old file kept once
the next one starts loading."""

import gc
import weakref
from array import array

import pytest

from carveracontroller.GcodeViewer import MESH_LINE_CHUNK, VERTEX_FLOAT_NUM, GCodeViewer, MeshManager


def _linedata(x, y, z, line_no, rapid=False):
    return [x, y, z, 0.0, 0.0 if rapid else 1.0, line_no, 1, 0.0 if rapid else 500.0, 0.0]


def _manager_with(count):
    manager = MeshManager()
    manager.add_data_arrs([_linedata(float(i), float(i % 7), 0.0, i + 1) for i in range(count)], True)
    return manager


def test_per_point_data_is_kept_in_typed_arrays():
    manager = _manager_with(10)
    for name in (
        "positions",
        "raw_positions",
        "lengths",
        "vertex_types",
        "raw_linenumbers",
        "raw_feed_rates",
        "raw_spindle_speeds",
        "raw_tools",
        "angles_of_vertices",
        "vertices",
    ):
        assert isinstance(getattr(manager, name), array), name
    assert manager.vertices.typecode == "f"
    assert manager.raw_linenumbers[3] == 4 and isinstance(manager.raw_linenumbers[3], int)
    assert isinstance(manager.raw_tools[0], int)


def test_meshes_are_views_into_the_vertices_not_copies():
    manager = _manager_with(MESH_LINE_CHUNK + 10)
    assert len(manager.meshes) == 2
    vertices, indices = manager.meshes[0]
    assert isinstance(vertices, memoryview) and vertices.obj is manager.vertices
    assert vertices.format == "f" and not vertices.readonly
    assert indices.format == "H" and not indices.readonly
    assert len(vertices) == MESH_LINE_CHUNK * VERTEX_FLOAT_NUM
    assert list(indices[:3]) == [0, 1, 2] and len(indices) == MESH_LINE_CHUNK
    # The second mesh starts on the first one's last point, so the line is
    # continuous, and both share one index array.
    second_vertices, second_indices = manager.meshes[1]
    assert len(second_vertices) == 11 * VERTEX_FLOAT_NUM
    assert len(second_indices) == 11
    assert second_indices.obj is indices.obj


def test_lengths_are_the_cumulative_distance_in_double_precision():
    manager = MeshManager()
    manager.add_data_arrs([_linedata(0.0, 0.0, 0.0, 1), _linedata(3.0, 4.0, 0.0, 2), _linedata(3.0, 4.0, 1e-6, 3)])
    scale = manager.position_scale
    assert list(manager.lengths) == pytest.approx([0.0, 5.0 * scale, (5.0 + 1e-6) * scale], rel=1e-12, abs=0)
    assert manager.lengths[2] > manager.lengths[1]


def test_clear_gives_new_arrays_and_leaves_the_old_ones_whole():
    manager = _manager_with(5)
    old_positions = manager.raw_positions
    old_view = manager.meshes[0][0]

    manager.clear()
    manager.add_data_arrs([_linedata(1.0, 1.0, 1.0, 1), _linedata(2.0, 2.0, 2.0, 2)], True)

    assert manager.raw_positions is not old_positions
    assert len(old_positions) == 15
    assert len(old_view) == 5 * VERTEX_FLOAT_NUM


def test_a_new_load_frees_the_old_files_points():
    """What load_start does before a new file loads (clearDisplay, then
    begin_new_file_load) must let go of every reference to the old file's
    per-point arrays, the drawn meshes' views included, so loading file
    after file does not keep the earlier ones in memory."""
    viewer = GCodeViewer()
    viewer.high_precision_time_estimate = False
    viewer.load_array([_linedata(float(i), float(i % 5), 0.0, i + 1) for i in range(500)], True)
    old_vertices = weakref.ref(viewer.meshmanager.vertices)
    old_positions = weakref.ref(viewer.raw_positions)
    old_lines = weakref.ref(viewer.raw_linenumbers)
    old_lengths = weakref.ref(viewer.lengths)

    viewer.clearDisplay(close_progress=False)
    viewer.begin_new_file_load()
    gc.collect()

    assert old_vertices() is None
    assert old_positions() is None
    assert old_lines() is None
    assert old_lengths() is None
