"""Two windows on the same screen -- this controller's own and a browser
demo view, say -- can show the same tool-change popup with nothing to say
which is which, or that confirming on the one without control takes it
from the other (found 3 Oct 2026, the motion session). Covers the two
places that now show this controller's own identity name:

  1. The window title.
  2. The tool-change popup's Confirm button, which names the controller
     control would move from when pressing it would move control here.
"""

from unittest.mock import patch

import pytest

from carveracontroller.main import tool_confirm_button_text, with_controller_name


@pytest.fixture(autouse=True)
def identity_translations():
    # Keep tr._ a no-op so tests assert on the untranslated text, the same
    # convention test_halt_estop_note.py uses.
    with patch("carveracontroller.main.tr._", side_effect=lambda s: s):
        yield


# -- with_controller_name (window title and popup heading) ------------------


def test_appends_the_name_to_the_base_text():
    assert with_controller_name("Carvera Controller Community v2.2.0", "Demo") == (
        "Carvera Controller Community v2.2.0 — Demo"
    )


def test_appends_the_name_to_a_popup_title():
    assert with_controller_name("Manual toolchange", "Workshop Laptop") == "Manual toolchange — Workshop Laptop"


def test_unchanged_when_name_is_empty():
    assert with_controller_name("Carvera Controller Community v2.2.0", "") == "Carvera Controller Community v2.2.0"


def test_unchanged_when_name_is_none():
    assert with_controller_name("Carvera Controller Community v2.2.0", None) == "Carvera Controller Community v2.2.0"


# -- tool_confirm_button_text (the popup's Confirm button) ------------------


def test_unchanged_while_this_controller_holds_control():
    text = tool_confirm_button_text(
        multi_user_mode=True,
        has_control=True,
        control_holder_id=1,
        control_holder_name="Workshop Laptop",
    )
    assert text == "Confirm"


def test_unchanged_in_single_user_mode_even_with_a_holder_named():
    # Single-user mode has no notion of control to take; can_write_machine_settings
    # treats it the same way.
    text = tool_confirm_button_text(
        multi_user_mode=False,
        has_control=False,
        control_holder_id=7,
        control_holder_name="Workshop Laptop",
    )
    assert text == "Confirm"


def test_unchanged_in_multi_user_mode_with_nobody_in_control():
    text = tool_confirm_button_text(
        multi_user_mode=True,
        has_control=False,
        control_holder_id=0,
        control_holder_name="",
    )
    assert text == "Confirm"


def test_names_the_holder_in_multi_user_mode_without_control():
    text = tool_confirm_button_text(
        multi_user_mode=True,
        has_control=False,
        control_holder_id=7,
        control_holder_name="Workshop Laptop",
    )
    assert text == "Confirm and take control from Workshop Laptop"


def test_falls_back_to_another_controller_when_the_holder_has_no_name():
    text = tool_confirm_button_text(
        multi_user_mode=True,
        has_control=False,
        control_holder_id=7,
        control_holder_name="",
    )
    assert text == "Confirm and take control from Another controller"
