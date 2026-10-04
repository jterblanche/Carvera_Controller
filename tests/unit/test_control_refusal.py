"""Unit tests for carveracontroller.machine.control_refusal."""

from carveracontroller.machine.control_refusal import is_control_refusal


def test_recognises_the_named_holder_variant():
    assert is_control_refusal("error:Refused -- PC has control") is True


def test_recognises_the_transfer_refused_variants():
    assert (
        is_control_refusal("error:Transfer refused -- Office PC has control and an interactive move is in progress")
        is True
    )
    assert is_control_refusal("error:Transfer refused -- an interactive move is in progress") is True


def test_is_case_insensitive_and_tolerates_surrounding_whitespace():
    assert is_control_refusal("  ERROR:REFUSED -- pc has control  ") is True


def test_is_not_fooled_by_an_unrelated_error_or_alarm():
    assert is_control_refusal("error:Line 12 - bad gcode") is False
    assert is_control_refusal("ALARM: Hard limit") is False
    assert is_control_refusal("ok") is False
    assert is_control_refusal("part.nc 1024 123456") is False


def test_handles_empty_or_none_input():
    assert is_control_refusal("") is False
    assert is_control_refusal(None) is False
