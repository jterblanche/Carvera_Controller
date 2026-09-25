"""The machine-settings entry for multi_client.passive_rights.

The firmware accepts exactly three spellings for this setting and treats
anything else as watch_only, reporting a config error at boot. The
settings page must offer exactly those three, with the firmware's own
default, or choosing a level here silently configures a different one.

A machine may still hold an older spelling (watch_pause_stop,
watch_pause_stop_upload) in its config.txt. The page must still open for
it, show that value as it is stored, and let the user replace it with one
of the valid ones.
"""

import json
from pathlib import Path

import pytest
from kivy.config import ConfigParser
from kivy.uix.settings import SettingOptions, Settings
from kivy.uix.togglebutton import ToggleButton

from carveracontroller.main import MACHINE_CONFIG_FILES, DeferredSettingsPanel

CONFIG_DIR = Path(__file__).resolve().parents[2] / "carveracontroller"
KEY = "multi_client.passive_rights"

# The firmware's accepted values, lowest to highest, and its default when
# the setting is absent from config.txt.
FIRMWARE_RIGHTS = ["watch_only", "watch_stop", "watch_stop_upload"]
FIRMWARE_DEFAULT = "watch_stop_upload"

# A firmware config value holds at most this many characters; a longer one
# is truncated on read and so can never match.
FIRMWARE_CONFIG_VALUE_MAX_CHARS = 19


def _passive_rights_entry(filename):
    data = json.loads((CONFIG_DIR / filename).read_text(encoding="utf-8"))
    entries = [entry for entry in data if entry.get("key") == KEY]
    assert len(entries) == 1, f"{filename}: expected exactly one {KEY} entry"
    return entries[0]


@pytest.mark.parametrize("filename", sorted(MACHINE_CONFIG_FILES.values()))
def test_offered_values_are_exactly_the_firmwares(filename):
    entry = _passive_rights_entry(filename)
    assert entry["type"] == "options"
    assert entry["options"] == FIRMWARE_RIGHTS
    assert entry["default"] == FIRMWARE_DEFAULT


@pytest.mark.parametrize("filename", sorted(MACHINE_CONFIG_FILES.values()))
def test_offered_values_fit_in_a_firmware_config_value(filename):
    entry = _passive_rights_entry(filename)
    for value in entry["options"]:
        assert len(value) <= FIRMWARE_CONFIG_VALUE_MAX_CHARS, value


def _build_page(filename, stored_value):
    """Build the settings page entry the way the controller does, for a
    machine whose config.txt holds `stored_value`. The controller drops
    `default` before building the page (Kivy's options widget has no such
    property), so this does too."""
    entry = dict(_passive_rights_entry(filename))
    entry.pop("default")
    config = ConfigParser()
    config.add_section(entry["section"])
    config.set(entry["section"], KEY, stored_value)

    panel = Settings().create_json_panel("Machine", config, data=json.dumps([entry]))
    panel.__class__ = DeferredSettingsPanel
    item = next(w for w in panel.walk() if isinstance(w, SettingOptions))
    return item


def _open_choices(item):
    item._create_popup(item)
    buttons = [w for w in item.popup.content.children if isinstance(w, ToggleButton)]
    return list(reversed(buttons))


@pytest.mark.parametrize("filename", sorted(MACHINE_CONFIG_FILES.values()))
@pytest.mark.parametrize("old_value", ["watch_pause_stop", "watch_pause_stop_upload"])
def test_old_stored_value_is_shown_as_stored_and_can_be_replaced(filename, old_value):
    item = _build_page(filename, old_value)

    # Shown as config.txt holds it, not silently mapped to a new name.
    assert item.value == old_value

    buttons = _open_choices(item)
    try:
        # Only valid values are offered, and none is marked as current,
        # because the stored value is none of them.
        assert [b.text for b in buttons] == FIRMWARE_RIGHTS
        assert all(b.state == "normal" for b in buttons)

        chosen = next(b for b in buttons if b.text == "watch_stop")
        chosen.state = "down"
        chosen.dispatch("on_release")
    finally:
        if item.popup is not None:
            item.popup.dismiss()

    assert item.value == "watch_stop"


@pytest.mark.parametrize("value", FIRMWARE_RIGHTS)
def test_valid_stored_value_is_marked_as_current(value):
    item = _build_page(MACHINE_CONFIG_FILES["CA1"], value)
    assert item.value == value

    buttons = _open_choices(item)
    try:
        assert [b.text for b in buttons if b.state == "down"] == [value]
    finally:
        item.popup.dismiss()
