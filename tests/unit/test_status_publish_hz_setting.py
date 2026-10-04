"""The machine-settings entry for multi_client.status_publish_hz.

The firmware clamps this setting to 1-50 Hz (Publish.cpp,
min_status_publish_hz/max_status_publish_hz) and falls back to its own
default of 5 Hz when the setting is absent or negative (Publish.h,
default_status_publish_hz; SerialConsole.cpp and WifiProvider.cpp both read
it with that fallback, once, in on_module_loaded/init, so a change only
takes effect after the machine restarts). The settings page must offer a
numeric entry with that same default, and its help text must say what the
setting does (how often the machine sends status to each connected
controller), that it only takes effect after a restart, and that a higher
value costs more WiFi bandwidth per controller.
"""

import json
from pathlib import Path

import pytest

from carveracontroller.main import MACHINE_CONFIG_FILES

CONFIG_DIR = Path(__file__).resolve().parents[2] / "carveracontroller"
KEY = "multi_client.status_publish_hz"

# The firmware's own clamp and default (src/libs/Publish.cpp,
# src/libs/Publish.h). Copied here by hand, as for
# multi_client.passive_rights in test_passive_rights_setting.py: the
# firmware is a separate repository the controller's tests cannot read.
FIRMWARE_MIN_HZ = 1
FIRMWARE_MAX_HZ = 50
FIRMWARE_DEFAULT_HZ = 5


def _status_publish_hz_entry(filename):
    data = json.loads((CONFIG_DIR / filename).read_text(encoding="utf-8"))
    entries = [entry for entry in data if entry.get("key") == KEY]
    assert len(entries) == 1, f"{filename}: expected exactly one {KEY} entry"
    return entries[0]


def _multi_client_mode_entry(filename):
    data = json.loads((CONFIG_DIR / filename).read_text(encoding="utf-8"))
    return next(entry for entry in data if entry.get("key") == "multi_client.mode")


@pytest.mark.parametrize("filename", sorted(MACHINE_CONFIG_FILES.values()))
def test_entry_is_numeric_with_firmwares_default(filename):
    entry = _status_publish_hz_entry(filename)
    assert entry["type"] == "numeric"
    assert entry["default"] == str(FIRMWARE_DEFAULT_HZ)


@pytest.mark.parametrize("filename", sorted(MACHINE_CONFIG_FILES.values()))
def test_help_text_states_range_restart_and_bandwidth_cost(filename):
    entry = _status_publish_hz_entry(filename)
    desc = entry["desc"]

    # The firmware's own clamp, so a value outside it is not a surprise.
    assert str(FIRMWARE_MIN_HZ) in desc
    assert str(FIRMWARE_MAX_HZ) in desc

    # It only takes effect after a restart: both transports read it once,
    # at load time, not on every status tick.
    assert "restart" in desc.lower()

    # Raising it costs WiFi bandwidth, per connected controller.
    assert "wifi" in desc.lower()
    assert "controller" in desc.lower()


@pytest.mark.parametrize("filename", sorted(MACHINE_CONFIG_FILES.values()))
def test_section_matches_the_other_multi_client_settings(filename):
    entry = _status_publish_hz_entry(filename)
    mode_entry = _multi_client_mode_entry(filename)
    assert entry["section"] == mode_entry["section"]
