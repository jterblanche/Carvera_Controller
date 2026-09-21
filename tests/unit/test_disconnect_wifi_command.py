"""Pin the exact text Controller.disconnectWiFiCommand sends.

The trailing word "disconnect" in "wlan -d disconnect" looks like a leftover
and is the obvious thing to tidy away, but it is load-bearing. The firmware's
SimpleShell::wlan_command (SimpleShell.cpp:1040) walks the parameters: "-e"
and "-d" are recognised as flags, and any other word becomes the SSID (or the
password, if an SSID was already taken). "disconnect" is that other word, so
it is stored as the SSID; its value is never read (the -d flag is what
selects the disconnect behaviour), but its presence keeps the SSID
non-empty. At line 1064 the command branches on ssid.empty(): the empty
branch is the wifi-scan path, and only the non-empty branch acts on -d and
disconnects. Deleting the trailing word would turn a disconnect into a scan.

A comment at the call site says the same thing; this test stops a confident
edit that reads the comment and still decides the word "isn't really used".
"""

from unittest.mock import MagicMock

from carveracontroller.Controller import Controller


def _controller_with_mock_execute():
    controller = Controller.__new__(Controller)  # skip __init__: no stream needed
    controller.executeCommand = MagicMock()
    return controller


def test_disconnect_wifi_command_sends_exact_string():
    controller = _controller_with_mock_execute()

    controller.disconnectWiFiCommand()

    controller.executeCommand.assert_called_once_with("wlan -d disconnect\n")
