"""The screen after the controller closes a link itself.

Controller._close_inline closes the link from the streamIO thread on a
rejected hello, a close before the controller identified, and a peer close
of a working session. It sets the state to NOT_CONNECTED, but the screen
only follows the state inside updateStatus, and no status report arrives on
a closed link. Each of the three popups shown afterwards must bring the
screen to its disconnected look, without offering to reconnect.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.main import NOT_CONNECTED, Makera

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")


def _connected_screen(controller) -> Makera:
    """A Makera whose status widgets still show a connected WiFi link."""
    root = Makera.__new__(Makera)
    root.controller = controller
    root.status_data_view = MagicMock()
    root.status_drop_down = MagicMock()
    for name in ("btn_connect_usb", "btn_connect_wifi", "btn_connect_network"):
        getattr(root.status_drop_down, name).disabled = True
    root.status_drop_down.btn_disconnect.disabled = False
    root.reconnection_popup = MagicMock()
    root.reconnection_popup._is_open = False
    root.camera_stream = MagicMock()
    root.gcode_viewer = MagicMock()
    root._md5_verify_event = MagicMock()
    root.show_message_popup = MagicMock()
    root._update_snapshot = None
    root.control_list = {
        name: [0, None] for name in ("feedrate_scale", "vacuum_mode", "laser_mode", "laser_test", "laser_scale")
    }
    return root


def _connected_app():
    return SimpleNamespace(
        state="Idle",
        playing=False,
        model="CA1",
        lasering=False,
        selected_remote_filename="",
        selected_local_filename="",
        has_anchor2=True,
        fw_version_digitized=0,
        is_community_firmware=False,
        supports_auto_ext_out=False,
        supports_camera=False,
        spindle_or_laser_is_on=False,
    )


@pytest.mark.parametrize(
    "popup, args",
    [
        ("show_hello_rejected_popup", ("old_controller",)),
        ("show_machine_busy_before_identify_popup", ()),
        ("show_peer_closed_popup", ()),
    ],
)
def test_popup_after_inline_close_shows_screen_disconnected(monkeypatch, connected_idle_state, popup, args):
    controller = Controller(CNC(), callback=None, identity=IDENTITY)
    controller.start_reconnection = MagicMock()
    app = _connected_app()
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: app)
    root = _connected_screen(controller)

    controller._close_inline()
    getattr(root, popup)(*args)

    assert app.state == NOT_CONNECTED
    assert root.status_data_view.main_text == NOT_CONNECTED
    assert root.status_drop_down.btn_disconnect.disabled is True
    assert root.status_drop_down.btn_connect_usb.disabled is False
    assert root.status_drop_down.btn_connect_wifi.disabled is False
    assert root.status_drop_down.btn_connect_network.disabled is False
    root.status_drop_down.set_connected_controllers.assert_called_once_with(())
    # The message itself is still shown.
    root.show_message_popup.assert_called_once()
    # _close_inline marks the close as deliberate, so the refresh must not
    # offer to reconnect to a machine that just closed the link.
    root.reconnection_popup.open.assert_not_called()
    controller.start_reconnection.assert_not_called()
