"""The identity a controller starts with: the computer part and a new
launch part, with the display name from the settings, and the random id
older versions kept in the settings removed."""

from __future__ import annotations

from unittest.mock import MagicMock

from carveracontroller import main as main_module
from carveracontroller.machine.identity import ControllerIdentity


class FakeConfigStore:
    def __init__(self, **values):
        self.values = dict(values)

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def remove(self, key):
        self.values.pop(key, None)


def test_start_up_builds_the_identity_from_the_computer_and_this_launch(monkeypatch):
    part = MagicMock(id=0x55)
    monkeypatch.setattr(main_module, "computer_part_for_this_process", lambda: part)
    monkeypatch.setattr(main_module, "new_launch_part", lambda: 0x66)
    store = FakeConfigStore(controller_id="42", controller_name="Shop Laptop")

    identity, got_part = main_module.build_controller_identity(store)

    assert identity == ControllerIdentity(id=0x55, name="Shop Laptop", launch=0x66)
    assert got_part is part
    assert "controller_id" not in store.values


def test_kivy_config_store_removes_the_old_id(monkeypatch):
    config = MagicMock()
    config.has_option.return_value = True
    monkeypatch.setattr(main_module, "Config", config)

    main_module._KivyConfigIdentityStore().remove("controller_id")

    config.remove_option.assert_called_once_with("carvera", "controller_id")
    config.write.assert_called_once()


def test_kivy_config_store_leaves_the_file_alone_when_there_is_no_old_id(monkeypatch):
    config = MagicMock()
    config.has_option.return_value = False
    monkeypatch.setattr(main_module, "Config", config)

    main_module._KivyConfigIdentityStore().remove("controller_id")

    config.remove_option.assert_not_called()
    config.write.assert_not_called()
