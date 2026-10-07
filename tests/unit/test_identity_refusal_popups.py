"""What the screen shows when the machine refuses a hello because this
identity is already connected (result 3) or because it stayed busy (result
4 for about 30 s), and "Make this a separate controller"."""

from __future__ import annotations

from unittest.mock import MagicMock

from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.machine.job_start import JobStartTracker
from carveracontroller.main import Makera

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC", launch=0xABCDEF)


class FakeConfirmPopup:
    def __init__(self):
        self.lb_title = MagicMock()
        self.lb_content = MagicMock()
        self.confirm_text = "Confirm"
        self.confirm = None
        self.cancel = None
        self.opened = False

    def reset_layout_defaults(self):
        self.confirm_text = "Confirm"

    def open(self, *args):
        self.opened = True


def _screen():
    root = Makera.__new__(Makera)
    root.identity = IDENTITY
    root.controller = MagicMock()
    root.controller.identity = IDENTITY
    root._job_start = JobStartTracker(own_id=IDENTITY.id)
    root.confirm_popup = FakeConfirmPopup()
    root.show_message_popup = MagicMock()
    root._refresh_after_inline_close = MagicMock()
    root.reconnect_last_connection = MagicMock()
    return root


def test_duplicate_refusal_explains_and_offers_a_separate_controller():
    root = _screen()

    root.show_hello_rejected_popup("duplicate")

    popup = root.confirm_popup
    assert popup.opened
    assert "Another controller with this computer's identity is already connected" in popup.lb_content.text
    assert "cloned" in popup.lb_content.text
    assert popup.confirm_text == "Make this a separate controller"
    assert popup.confirm == root.make_separate_controller
    root.show_message_popup.assert_not_called()
    root._refresh_after_inline_close.assert_called_once()


def test_busy_refusal_says_the_machine_is_busy_not_that_the_identity_is_in_use():
    root = _screen()

    root.show_hello_rejected_popup("busy")

    root.show_message_popup.assert_called_once()
    message = root.show_message_popup.call_args.args[0]
    assert "busy" in message
    assert "identity" not in message
    assert "separate" not in message
    assert not root.confirm_popup.opened


def test_existing_refusals_are_unchanged():
    root = _screen()

    root.show_hello_rejected_popup("cap")
    root.show_hello_rejected_popup("old_controller")

    first, second = (call.args[0] for call in root.show_message_popup.call_args_list)
    assert first == "This machine already has the maximum number of controllers connected."
    assert second.startswith("An older controller is connected to this machine.")


def test_make_separate_controller_gives_a_new_id_everywhere_and_connects_again():
    root = _screen()
    part = MagicMock()
    separated = MagicMock(id=0x0F0E0D0C0B0A0908)
    part.made_separate.return_value = separated
    root._computer_part = part

    root.make_separate_controller()

    new = ControllerIdentity(id=0x0F0E0D0C0B0A0908, name="Test PC", launch=0xABCDEF)
    assert root.identity == new
    assert root.controller.identity == new
    assert root._job_start.own_id == new.id
    assert root._computer_part is separated
    root.reconnect_last_connection.assert_called_once()


def test_make_separate_controller_that_cannot_store_its_value_says_so():
    root = _screen()
    part = MagicMock()
    part.made_separate.side_effect = OSError("read-only")
    root._computer_part = part

    root.make_separate_controller()

    assert root.identity == IDENTITY
    root.show_message_popup.assert_called_once()
    root.reconnect_last_connection.assert_not_called()
