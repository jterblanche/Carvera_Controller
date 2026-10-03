"""The status drop-down's connected-controllers rows and the Release
Control wrap: the list was hard to read, and "Release Control"
overflowed the menu on both the laptop and the demo.

Most of this file tests pure logic (StatusDropDown.set_connected_controllers
building one widget per controller, with the right text and colour) on a
bare instance, the same way other main.py unit tests here avoid a full
Kivy widget tree. The wrap/width checks need the real `makera.kv` rules
applied, since the point is what the kv file does to the widget's
properties -- so those load the actual file, with a stand-in app for the
one unrelated `app.state` binding inside the same drop-down's rule.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from kivy.app import App
from kivy.lang import Builder
from kivy.metrics import dp

from carveracontroller.machine.clients import ClientRow
from carveracontroller.main import ConnectedControllerRow, StatusDropDown

KV_PATH = Path(__file__).resolve().parents[2] / "carveracontroller" / "makera.kv"


def _row(name, *, is_self=False, has_control=False):
    return ClientRow(name=name, has_control=has_control, is_self=is_self)


def _bare_drop_down():
    """A StatusDropDown without running its (or ToolTipDropDown's) __init__
    -- no kv, no Window binding -- with just the container
    set_connected_controllers needs. Mirrors test_apply_bed_settings.py's
    and test_screen_after_inline_close.py's use of __new__ elsewhere in
    this suite."""
    from kivy.uix.boxlayout import BoxLayout

    drop_down = StatusDropDown.__new__(StatusDropDown)
    drop_down.connected_controllers_container = BoxLayout(orientation="vertical")
    return drop_down


# -- set_connected_controllers: one row per controller --------------------


def test_set_connected_controllers_builds_one_row_per_controller():
    drop_down = _bare_drop_down()
    rows = (
        _row("Office PC", is_self=True, has_control=True),
        _row("Workshop Laptop", is_self=False, has_control=False),
    )

    drop_down.set_connected_controllers(rows)

    children = list(reversed(drop_down.connected_controllers_container.children))
    assert len(children) == 2
    assert all(isinstance(child, ConnectedControllerRow) for child in children)
    assert children[0].text == "Office PC (you) — in control"
    assert children[0].has_control is True
    assert children[1].text == "Workshop Laptop"
    assert children[1].has_control is False


def test_set_connected_controllers_truncates_a_long_name_with_an_ellipsis():
    drop_down = _bare_drop_down()
    long_name = "A Very Long Controller Name Indeed"  # well over the 31-byte wire cap

    drop_down.set_connected_controllers((_row(long_name),))

    row = drop_down.connected_controllers_container.children[0]
    assert row.text.endswith("…")
    assert len(row.text) < len(long_name)
    # Still one line: no newline for a wrapped second line to be mistaken
    # for a separate controller.
    assert "\n" not in row.text


def test_set_connected_controllers_empty_list_shows_nothing():
    drop_down = _bare_drop_down()
    drop_down.set_connected_controllers((_row("Office PC"),))

    drop_down.set_connected_controllers(())

    assert drop_down.connected_controllers_container.children == []


def test_set_connected_controllers_replaces_rather_than_appends():
    drop_down = _bare_drop_down()
    drop_down.set_connected_controllers((_row("Office PC"),))

    drop_down.set_connected_controllers((_row("Office PC"), _row("Workshop Laptop")))

    assert len(drop_down.connected_controllers_container.children) == 2


# -- kv-applied properties: the actual wrap and width fix ------------------


@pytest.fixture(scope="module", autouse=True)
def _load_makera_kv():
    if str(KV_PATH) not in Builder.files:
        Builder.load_file(str(KV_PATH))


@pytest.fixture
def _stub_running_app(monkeypatch):
    """<StatusDropDown>'s kv rule reads app.state for its unrelated
    Reconnect button, evaluated the moment the widget is built. Not part
    of this fix; just needs a stand-in so building the real widget tree
    doesn't need a full running App."""
    stub = SimpleNamespace(state="N/A")
    monkeypatch.setattr(App, "get_running_app", staticmethod(lambda: stub))
    return stub


def test_status_drop_down_has_a_fixed_width_wide_enough_for_long_rows(_stub_running_app):
    drop_down = StatusDropDown()
    assert drop_down.auto_width is False
    assert drop_down.width >= dp(220)


def test_release_control_label_is_bound_to_wrap_within_its_own_width(_stub_running_app):
    drop_down = StatusDropDown()
    button = drop_down.btn_release_control

    assert button.halign == "center"
    # text_size[0] not None is what makes Kivy wrap instead of overflowing;
    # None (the Button/Label default) means "no limit", i.e. one long line.
    assert button.text_size[0] is not None
    # And it tracks the button's own width, not a fixed guess, so it still
    # wraps correctly if the menu width ever changes.
    button.width = 240
    assert button.text_size[0] == pytest.approx(240 - dp(16))


def test_release_control_height_grows_with_wrapped_text(_stub_running_app):
    drop_down = StatusDropDown()
    button = drop_down.btn_release_control

    drop_down.can_release_control = True
    assert button.height == pytest.approx(button.texture_size[1] + dp(16))

    drop_down.can_release_control = False
    assert button.height == 0
