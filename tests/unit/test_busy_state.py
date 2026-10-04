"""Unit tests for carveracontroller.machine.busy_state."""

import pytest

from carveracontroller.machine.busy_state import machine_is_busy


@pytest.mark.parametrize("state", ["Idle", "Alarm", "Sleep"])
def test_idle_like_states_are_not_busy(state):
    assert machine_is_busy(state) is False


@pytest.mark.parametrize(
    "state",
    ["Run", "Hold", "Home", "Wait", "Tool", "Pause", "N/A", "", "SomeUnknownState"],
)
def test_everything_else_is_busy(state):
    assert machine_is_busy(state) is True
