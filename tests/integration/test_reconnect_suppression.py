"""Integration test: _manual_disconnect suppresses updateStatus's own
reconnect-popup logic — the main.py-level half of the busy/rejected-ack
fixes in Controller._close_inline. A Controller-level test can't exercise
this: the bug it guards against is in main.py's UI reaction to the state
change _close_inline causes, not in Controller state itself.
"""

from kivy.config import Config

from carveracontroller.CNC import CNC
from carveracontroller.Controller import NOT_CONNECTED
from tests.integration.conftest import apply_machine_state


class TestReconnectSuppression:
    def test_manual_disconnect_flag_suppresses_the_reconnect_popup(self, kivy_app):
        root = kivy_app.root
        controller = root.controller
        popup = root.reconnection_popup
        try:
            CNC.vars["state"] = "Idle"
            apply_machine_state(kivy_app)
            assert kivy_app.state == "Idle"  # sanity: the transition below is real

            # What Controller._close_inline sets before the busy/rejected
            # popup is shown.
            controller._manual_disconnect = True
            controller.stream = None
            CNC.vars["state"] = NOT_CONNECTED
            apply_machine_state(kivy_app)

            assert kivy_app.state == NOT_CONNECTED
            assert popup._is_open is False
        finally:
            if popup._is_open:
                popup.dismiss()
            controller._manual_disconnect = False
            CNC.vars["state"] = NOT_CONNECTED
            apply_machine_state(kivy_app)

    def test_without_the_flag_the_same_transition_opens_it(self, kivy_app):
        """Contrast case, proving the test above isn't vacuous: the
        identical state transition, without _manual_disconnect, does open
        the popup. auto_reconnect stays off for this one so opening it
        doesn't also arm a real background reconnect timer."""
        root = kivy_app.root
        controller = root.controller
        popup = root.reconnection_popup
        previous_auto_reconnect = Config.get("carvera", "auto_reconnect_enabled")
        Config.set("carvera", "auto_reconnect_enabled", "0")
        try:
            CNC.vars["state"] = "Idle"
            apply_machine_state(kivy_app)
            assert kivy_app.state == "Idle"

            controller._manual_disconnect = False
            controller.stream = None
            CNC.vars["state"] = NOT_CONNECTED
            apply_machine_state(kivy_app)

            assert kivy_app.state == NOT_CONNECTED
            assert popup._is_open is True
        finally:
            Config.set("carvera", "auto_reconnect_enabled", previous_auto_reconnect)
            if popup._is_open:
                popup.dismiss()
            controller._manual_disconnect = False
            CNC.vars["state"] = NOT_CONNECTED
            apply_machine_state(kivy_app)
