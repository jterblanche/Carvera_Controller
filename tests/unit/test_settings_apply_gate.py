"""The settings page's Apply button: disabled when there is nothing to
apply, and when machine setting changes are pending that the machine would
refuse because another controller holds control (multi-user mode). Changes
to this controller's own settings never reach the machine, so they can
always be applied on their own."""

import pytest

from carveracontroller.main import settings_apply_disabled


@pytest.mark.parametrize(
    "controller_changes, machine_changes, writable, disabled",
    [
        (False, False, True, True),
        (False, False, False, True),
        (True, False, True, False),
        (True, False, False, False),
        (False, True, True, False),
        (False, True, False, True),
        (True, True, True, False),
        (True, True, False, True),
    ],
)
def test_settings_apply_disabled(controller_changes, machine_changes, writable, disabled):
    assert settings_apply_disabled(controller_changes, machine_changes, writable) is disabled
