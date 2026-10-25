"""Unit tests for carveracontroller.machine.control_refusal."""

from carveracontroller.machine.control_refusal import is_control_refusal, refusal_reason


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


def test_recognises_the_busy_refusal():
    # Firmware's own config-write gate refuses config-set,
    # config-restore and config-default the same way: an "error:Refused"
    # line. Nothing here needs to be busy-specific -- the generic
    # "error:Refused" shape this module already matches covers it.
    assert is_control_refusal("error:Refused -- can't change settings while the machine is busy") is True


def test_refusal_reason_is_the_text_after_the_dashes():
    assert refusal_reason("error:Refused -- Auto 1 has control") == "Auto 1 has control"
    assert (
        refusal_reason("error:Transfer refused -- PC has control and an interactive move is in progress")
        == "PC has control and an interactive move is in progress"
    )
    assert refusal_reason("  error:Refused -- a job is about to start \r\n") == "a job is about to start"


def test_refusal_reason_without_dashes_is_the_whole_line():
    assert refusal_reason("error:Refused") == "error:Refused"
    assert refusal_reason("") == ""
