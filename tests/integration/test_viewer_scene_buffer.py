"""The toolpath view is drawn again only when the view itself changes.

Kivy redraws the whole window whenever anything on it changes. The viewer
keeps its 3D scene in an off-screen buffer, so a redraw caused by something
else on the window (a status colour, a label) copies the buffer instead of
drawing the toolpath again, which is what made a large file slow the whole
controller down.
"""

import os

from kivy.core.window import Window
from kivy.graphics import Callback

from tests.integration.conftest import load_gcode_file, pump_frames

_TESTS_DIR = os.path.join(os.path.dirname(__file__), "..")
GCODE_FILE = os.path.join(_TESTS_DIR, "resources", "Face 4x4 stock.cnc")


def _count_scene_draws(viewer):
    """Add a callback to the scene buffer that counts its draws. Adding it
    marks the buffer for one draw, so settle the window before counting."""
    draws = []
    marker = Callback(lambda *_args: draws.append(1))
    viewer._scene.add(marker)
    pump_frames(5)
    draws.clear()
    return draws, marker


def _count_window_draws():
    flips = []

    def on_flip(*_args):
        flips.append(1)

    Window.bind(on_flip=on_flip)
    return flips, on_flip


class TestViewerSceneBuffer:
    def test_scene_is_drawn_off_screen(self, kivy_app):
        kivy_app.root.content.current = "File"
        load_gcode_file(kivy_app, GCODE_FILE)
        viewer = kivy_app.root.gcode_viewer

        # The toolpath, grid and tool are in the buffer, not in the widget's
        # own canvas, which only shows the buffer.
        assert viewer.linemesh in viewer._scene.children
        assert viewer.gridmesh in viewer._scene.children
        assert viewer.pointermesh in viewer._scene.children
        assert viewer.linemesh not in viewer.canvas.children
        assert viewer._scene in viewer.canvas.children
        assert viewer._scene_rect.texture is viewer._scene.texture

    def test_buffer_matches_the_viewer_size(self, kivy_app):
        kivy_app.root.content.current = "File"
        pump_frames(5)
        viewer = kivy_app.root.gcode_viewer
        assert tuple(viewer._scene.size) == (int(viewer.width), int(viewer.height))
        assert tuple(viewer._scene_rect.size) == tuple(viewer._scene.size)
        # Shown on whole window pixels, at most a pixel from the widget.
        rect_x, rect_y = viewer._scene_rect.pos
        win_x, win_y = viewer.to_window(rect_x, rect_y)
        assert abs(win_x - round(win_x)) < 1e-6 and abs(win_y - round(win_y)) < 1e-6
        assert -1e-6 <= viewer.x - rect_x < 1 and -1e-6 <= viewer.y - rect_y < 1

    def test_other_window_changes_do_not_redraw_the_scene(self, kivy_app):
        kivy_app.root.content.current = "File"
        load_gcode_file(kivy_app, GCODE_FILE)
        viewer = kivy_app.root.gcode_viewer
        status = kivy_app.root.status_data_view
        draws, marker = _count_scene_draws(viewer)
        flips, on_flip = _count_window_draws()
        try:
            # What blink_state does twice a second while the machine holds.
            for color in ((1, 0, 0, 1), (0, 1, 0, 1), (0, 0, 1, 1)):
                status.color = color
                pump_frames(2, sleep=0.02)
            assert flips, "the window should have been redrawn"
            assert draws == []
        finally:
            Window.unbind(on_flip=on_flip)
            viewer._scene.remove(marker)
            pump_frames(2)

    def test_scene_changes_redraw_the_scene(self, kivy_app):
        kivy_app.root.content.current = "File"
        load_gcode_file(kivy_app, GCODE_FILE)
        viewer = kivy_app.root.gcode_viewer
        draws, marker = _count_scene_draws(viewer)
        try:
            viewer.zoom_in()
            pump_frames(3, sleep=0.02)
            assert len(draws) >= 1
            # And once drawn, it stays put while nothing changes.
            draws.clear()
            pump_frames(5, sleep=0.02)
            assert draws == []
        finally:
            viewer._scene.remove(marker)
            pump_frames(2)
