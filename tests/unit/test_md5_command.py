"""Pin the exact text Controller.md5Command sends.

The firmware's "md5sum" takes an absolute path and nothing else
(SimpleShell::md5sum_command hands the whole remainder of the line straight
to absolute_from_relative and opens the result as a filename) -- unlike
ls/cat/rm/mv/mkdir, it never splits a trailing "-e" off with
shift_parameter. Appending one used to turn it into part of the filename, so
the machine answered "File not found" for a file that was actually there.
The whole bug was in the string built here, so these tests assert on the
exact text, not a regex or a substring.

For the same reason -- no shift_parameter -- md5sum does not decode the 0x01
stand-in for a space. The space test below therefore pins the escaping the
command has always done, not a path the machine would resolve; the paths
this is used with contain no spaces.
"""

from unittest.mock import MagicMock

from carveracontroller.Controller import Controller


def _controller_with_mock_execute():
    controller = Controller.__new__(Controller)  # skip __init__: no stream needed
    controller.executeCommand = MagicMock()
    return controller


def test_md5_command_sends_bare_path_with_no_flags():
    controller = _controller_with_mock_execute()

    controller.md5Command("/sd/firmware.bin")

    controller.executeCommand.assert_called_once_with("md5sum /sd/firmware.bin\n")


def test_md5_command_keeps_the_existing_space_escaping():
    controller = _controller_with_mock_execute()

    controller.md5Command("/sd/gcodes/my job.nc")

    controller.executeCommand.assert_called_once_with("md5sum /sd/gcodes/my\x01job.nc\n")


def test_md5_command_normalises_windows_style_backslashes():
    controller = _controller_with_mock_execute()

    controller.md5Command("\\sd\\gcodes\\my job.nc")

    controller.executeCommand.assert_called_once_with("md5sum /sd/gcodes/my\x01job.nc\n")
