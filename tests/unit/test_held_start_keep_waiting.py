"""A held start whose wait has run out (job-start phase 4, timed out).

A machine that supports it (hello ack feature bit 0x02) no longer starts a
held job when its time limit runs out while a controller that takes part is
still not synced, as long as the controller that pressed Play said in its
hello that it can choose (feature bit 0x02). It keeps the job held, sends
phase 4 once a second, and waits for that controller: Start now, Keep
waiting (the ``keep-waiting`` command, which restarts the full wait and
returns to phase waiting) or Cancel. The job still starts by itself once
every awaited controller has synced. These tests cover the hello bit, the
phase, the tracker, and the dialog on the starter and on the others.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from carveracontroller.machine.clients import list_label
from carveracontroller.machine.hello import HelloNegotiator
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.machine.job_start import (
    MIN_SECONDS_LEFT_TO_RETRY,
    STALE_AFTER_LIMIT_S,
    JobStartAction,
    JobStartTracker,
)
from carveracontroller.main import Makera
from carveracontroller.protocols.handshake import (
    HELLO_ACCEPTED,
    HELLO_FEATURE_JOB_START_WAIT,
    HELLO_FEATURE_KEEP_WAITING,
    JOB_START_REASON_ALL_READY,
    JOB_START_REASON_START_NOW,
    JOB_START_STARTING,
    JOB_START_TIMED_OUT,
    JOB_START_WAITING,
    LINK_USB,
    LINK_WIFI,
    ClientEntry,
    HelloAck,
    decode_job_start_event,
)
from carveracontroller.protocols.makera import HELLO_PROTOCOL_VERSION
from carveracontroller.protocols.messages import MessageKind, ParsedMessage
from tests.unit import test_job_start_sync
from tests.unit.test_job_start_sync import (
    DEMO,
    PC,
    THIRD,
    _controller,
    _event,
    _host,
    _logged,
    _PopupWithButtons,
)
from tests.unit.test_job_start_wire import _parse, job_start_payload

# The running-app fixture of the job-start sync tests.
app = test_job_start_sync.app


def _timed_out(**kw):
    kw.setdefault("not_ready", (DEMO,))
    return _event(phase=JOB_START_TIMED_OUT, seconds_left=0, **kw)


def _starter(tmp_path, keep_waiting=True, **kw):
    root = _host(tmp_path, own_id=PC, has_control=True, **kw)
    root.controller.machine_supports_keep_waiting = keep_waiting
    root.controller.keepWaitingCommand = MagicMock()
    return root


# -- wire and hello -------------------------------------------------------------


def test_phase_and_feature_values_are_the_contracts():
    assert JOB_START_TIMED_OUT == 4
    assert HELLO_FEATURE_KEEP_WAITING == 0x02


def test_timed_out_event_decodes():
    event = decode_job_start_event(
        job_start_payload(phase=JOB_START_TIMED_OUT, seconds_left=0, not_ready=(0x2222, 0x3333))
    )
    assert event is not None
    assert event.phase == JOB_START_TIMED_OUT
    assert event.seconds_left == 0
    assert event.starter_id == 0x1111
    assert event.not_ready_ids == (0x2222, 0x3333)


def test_the_hello_says_this_controller_can_choose_when_the_wait_runs_out():
    negotiator = HelloNegotiator(identity=ControllerIdentity(id=5, name="Demo"), link=0)
    parsed = _parse(negotiator.on_valid_frame(0.0))
    assert parsed.payload[-1] == HELLO_FEATURE_JOB_START_WAIT | HELLO_FEATURE_KEEP_WAITING


def _negotiated(features):
    negotiator = HelloNegotiator(identity=ControllerIdentity(id=5, name="Demo"), link=0)
    negotiator.on_valid_frame(0.0)
    negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_ACCEPTED, 0, features))
    return negotiator


def test_keep_waiting_needs_the_machines_feature_bit():
    assert _negotiated(HELLO_FEATURE_JOB_START_WAIT | HELLO_FEATURE_KEEP_WAITING).machine_supports_keep_waiting
    assert not _negotiated(HELLO_FEATURE_JOB_START_WAIT).machine_supports_keep_waiting
    assert not HelloNegotiator(identity=ControllerIdentity(id=5, name="Demo"), link=0).machine_supports_keep_waiting


def test_controller_reports_the_machines_keep_waiting_bit(monkeypatch):
    c, _ = _controller(monkeypatch)
    c._hello = _negotiated(HELLO_FEATURE_JOB_START_WAIT | HELLO_FEATURE_KEEP_WAITING)
    assert c.machine_supports_keep_waiting is True
    c._hello = _negotiated(HELLO_FEATURE_JOB_START_WAIT)
    assert c.machine_supports_keep_waiting is False
    c._hello = None
    assert c.machine_supports_keep_waiting is False


def test_keep_waiting_sends_the_keep_waiting_command(monkeypatch):
    c, _ = _controller(monkeypatch)
    c.executeCommand = MagicMock()
    c.keepWaitingCommand()
    c.executeCommand.assert_called_once_with("keep-waiting\n")


def test_controller_hands_a_timed_out_event_to_the_ui(monkeypatch):
    c, _ = _controller(monkeypatch)
    handed = []
    c._notify_job_start = handed.append

    c._handle_protocol_message(
        ParsedMessage(MessageKind.EVENT, payload=job_start_payload(phase=JOB_START_TIMED_OUT, seconds_left=0))
    )
    assert [e.phase for e in handed] == [JOB_START_TIMED_OUT]


# -- tracker --------------------------------------------------------------------


def test_timed_out_keeps_the_start_held():
    t = JobStartTracker(own_id=PC)
    t.on_event(_event(not_ready=(DEMO,)), now=0.0)
    assert t.on_event(_timed_out(), now=30.0) is JobStartAction.NONE
    assert t.held
    assert t.timed_out
    assert t.is_starter
    assert t.seconds_left(30.0) == 0


def test_keep_waiting_returns_to_the_countdown():
    t = JobStartTracker(own_id=PC)
    t.on_event(_timed_out(), now=30.0)
    t.on_event(_event(not_ready=(DEMO,), seconds_left=30), now=45.0)
    assert not t.timed_out
    assert t.seconds_left(45.0) == 30


def test_a_controller_still_listed_after_the_wait_ran_out_keeps_preparing():
    """The job starts by itself once every awaited controller syncs, so a
    controller still listed goes on fetching and sending ready."""
    t = JobStartTracker(own_id=DEMO)
    assert t.on_event(_timed_out(), now=30.0) is JobStartAction.PREPARE
    t.begin_preparing(7)
    assert t.on_event(_timed_out(), now=31.0) is JobStartAction.NONE
    assert t.prepared(7) is True
    assert t.on_event(_timed_out(), now=32.0) is JobStartAction.RESEND_READY


def test_a_failed_attempt_is_retried_after_the_wait_ran_out():
    """The time-left rule stops retries near the limit; once the start is
    held past it there is no limit, so attempts go on after the backoff."""
    t = JobStartTracker(own_id=DEMO)
    t.on_event(_event(seconds_left=MIN_SECONDS_LEFT_TO_RETRY + 5), now=0.0)
    t.begin_preparing(7)
    t.failed(7, now=1.0)
    assert t.on_event(_event(seconds_left=MIN_SECONDS_LEFT_TO_RETRY - 1), now=6.0) is JobStartAction.NONE
    assert t.on_event(_timed_out(), now=9.0) is JobStartAction.PREPARE


def test_a_timed_out_hold_goes_stale_only_when_its_events_stop():
    """Phase 4 comes once a second with no limit to count to: the hold is
    dropped when nothing has come for STALE_AFTER_LIMIT_S, but not while a
    file transfer is running (the machine sends no events then)."""
    t = JobStartTracker(own_id=DEMO)
    t.on_event(_timed_out(), now=100.0)
    t.on_event(_timed_out(), now=500.0)
    assert not t.stale(500.0 + STALE_AFTER_LIMIT_S)
    assert t.stale(500.0 + STALE_AFTER_LIMIT_S + 1)
    assert not t.stale(500.0 + STALE_AFTER_LIMIT_S + 100, transfer_running=True)


# -- the dialog on the controller that pressed Play ----------------------------


def test_the_starter_gets_start_now_keep_waiting_and_cancel(tmp_path, app):
    root = _starter(tmp_path)
    Makera.on_job_start_event(root, _event(not_ready=(DEMO, THIRD)))
    Makera.on_job_start_event(root, _timed_out(not_ready=(DEMO, THIRD)))

    popup = root._job_start_popup
    assert popup.showing
    assert popup.buttons[-1] is True
    assert popup.keep_waitings[-1] is True
    assert popup.cancels[-1] is True
    text = popup.texts[-1]
    assert "ran out" in text
    # Named by their labels in the connected-controllers list.
    assert "Not synced yet: Demo (WiFi), Shop (WiFi)." in text
    assert "Start now" in text and "Keep waiting" in text and "Cancel" in text
    assert popup.titles[-1] != popup.titles[0]


def test_keep_waiting_button_sends_keep_waiting_and_the_countdown_comes_back(tmp_path, app, monkeypatch):
    _PopupWithButtons.made = []
    monkeypatch.setattr("carveracontroller.main.JobStartPopup", _PopupWithButtons)
    root = _starter(tmp_path)
    root._job_start_popup = None

    Makera.on_job_start_event(root, _timed_out())
    popup = _PopupWithButtons.made[0]
    popup.on_keep_waiting()
    root.controller.keepWaitingCommand.assert_called_once_with()
    root.controller.startNowCommand.assert_not_called()
    root.controller.abortCommand.assert_not_called()
    # Nothing changes until the machine says so.
    assert "ran out" in popup.texts[-1]

    Makera.on_job_start_event(root, _event(not_ready=(DEMO,), seconds_left=30))
    assert popup.showing
    assert "will start in 30 s" in popup.texts[-1]
    assert popup.keep_waitings[-1] is False
    assert popup.buttons[-1] is True


def test_start_now_and_cancel_from_the_timed_out_dialog(tmp_path, app, monkeypatch):
    _PopupWithButtons.made = []
    monkeypatch.setattr("carveracontroller.main.JobStartPopup", _PopupWithButtons)
    root = _starter(tmp_path)
    root._job_start_popup = None
    Makera.on_job_start_event(root, _timed_out())
    popup = _PopupWithButtons.made[0]

    popup.on_start_now()
    popup.on_cancel()
    root.controller.startNowCommand.assert_called_once_with()
    root.controller.abortCommand.assert_called_once_with()


def test_the_timed_out_dialog_is_refreshed_every_second(tmp_path, app):
    root = _starter(tmp_path)
    Makera.on_job_start_event(root, _timed_out())
    Makera._tick_job_start_countdown(root)
    assert root._job_start_popup.showing
    assert "ran out" in root._job_start_popup.texts[-1]
    assert root._job_start.held


def test_everyone_synced_after_the_wait_ran_out_starts_the_job(tmp_path, app):
    root = _starter(tmp_path)
    Makera.on_job_start_event(root, _timed_out())
    Makera.on_job_start_event(
        root, _event(phase=JOB_START_STARTING, reason=JOB_START_REASON_ALL_READY, seconds_left=0, not_ready=())
    )
    assert not root._job_start_popup.showing
    assert not root._job_start.held
    assert not any("without waiting" in text for text in _logged(root))


def test_start_now_after_the_wait_ran_out_names_who_was_not_waited_for(tmp_path, app):
    root = _starter(tmp_path)
    Makera.on_job_start_event(root, _timed_out(not_ready=(THIRD,)))
    Makera.on_job_start_event(
        root, _event(phase=JOB_START_STARTING, reason=JOB_START_REASON_START_NOW, seconds_left=0, not_ready=())
    )
    assert any("without waiting for" in text and "Shop" in text for text in _logged(root))


def test_without_the_machines_keep_waiting_bit_there_is_no_keep_waiting(tmp_path, app):
    """A machine without bit 0x02 starts at the limit as before and never
    sends phase 4; should one arrive anyway, Keep waiting is not offered."""
    root = _starter(tmp_path, keep_waiting=False)
    Makera.on_job_start_event(root, _event(not_ready=(DEMO,)))
    assert root._job_start_popup.keep_waitings == [False]
    Makera.on_job_start_event(root, _timed_out())
    assert root._job_start_popup.keep_waitings[-1] is False
    assert "Keep waiting" not in root._job_start_popup.texts[-1]


def test_the_countdown_never_offers_keep_waiting(tmp_path, app):
    root = _starter(tmp_path)
    Makera.on_job_start_event(root, _event(phase=JOB_START_WAITING, not_ready=(DEMO,)))
    assert root._job_start_popup.keep_waitings == [False]
    assert "Keep waiting" not in root._job_start_popup.texts[-1]


# -- the dialog on every other controller ---------------------------------------


def test_a_watcher_sees_the_state_and_who_is_not_synced_without_the_choice(tmp_path, app):
    root = _host(tmp_path, own_id=THIRD, rights="watch_only")
    Makera.on_job_start_event(root, _timed_out(not_ready=(DEMO,)))

    popup = root._job_start_popup
    assert popup.showing
    assert popup.buttons == [False]
    assert popup.keep_waitings == [False]
    assert popup.cancels == [False]
    text = popup.texts[-1]
    assert "ran out" in text
    assert "Not synced yet: Demo (WiFi)." in text
    assert "PC (USB) pressed Play" in text  # who chooses
    assert "Keep waiting" not in text and "Start now" not in text and "Cancel" not in text


def test_a_watcher_keeps_its_cancel(tmp_path, app):
    root = _host(tmp_path, own_id=THIRD, rights="watch_stop")
    Makera.on_job_start_event(root, _timed_out(not_ready=(DEMO,)))
    popup = root._job_start_popup
    assert popup.cancels == [True]
    assert popup.buttons == [False]
    assert popup.keep_waitings == [False]
    assert "Cancel" in popup.texts[-1]


def test_a_watcher_still_listed_names_itself_and_keeps_fetching(tmp_path, app):
    root = _host(tmp_path)  # DEMO, no local copy
    Makera.on_job_start_event(root, _timed_out(not_ready=(DEMO,)))
    root.doDownload.assert_called_once()
    root.controller.send_job_start_ready.assert_called_once_with(7)
    assert "Not synced yet: this controller." in root._job_start_popup.texts[0]


# -- the real popup ---------------------------------------------------------------


def test_the_popup_puts_keep_waiting_between_start_now_and_cancel():
    from carveracontroller.ui.job_start_popup import JobStartPopup

    on_keep_waiting = MagicMock()
    popup = JobStartPopup(on_start_now=MagicMock(), on_cancel=MagicMock(), on_keep_waiting=on_keep_waiting)
    popup.showing = True  # never opened in a test

    popup.update("text", show_start_now=True, show_cancel=True, show_keep_waiting=True, title="Held")
    assert list(reversed(popup.buttons.children)) == [popup.btn_start_now, popup.btn_keep_waiting, popup.btn_cancel]
    assert popup.title == "Held"
    popup.btn_keep_waiting.dispatch("on_release")
    on_keep_waiting.assert_called_once_with()

    popup.update("text", show_start_now=True, show_cancel=True)
    assert list(reversed(popup.buttons.children)) == [popup.btn_start_now, popup.btn_cancel]
    assert popup.title == "Job about to start"


def test_list_label_is_the_drop_down_row_without_its_marks():
    usb = ClientEntry(id=1, name="Workshop PC", link=LINK_USB, has_control=True)
    a = ClientEntry(id=2, name="Laptop", link=LINK_WIFI, has_control=False, ip="192.168.1.20")
    b = ClientEntry(id=3, name="Laptop", link=LINK_WIFI, has_control=False, ip="192.168.1.21")
    entries = (usb, a, b)
    assert list_label(usb, entries) == "Workshop PC (USB)"
    assert list_label(a, entries) == "Laptop (WiFi - 192.168.1.20)"
    assert list_label(a, (usb, a)) == "Laptop (WiFi)"


# -- the wait runs out while another controller's file transfer runs -------------


def _transfer_host(tmp_path, monkeypatch, own_id=PC, **kw):
    clock = [100.0]
    monkeypatch.setattr("carveracontroller.main.time.monotonic", lambda: clock[0])
    _PopupWithButtons.made = []
    monkeypatch.setattr("carveracontroller.main.JobStartPopup", _PopupWithButtons)
    root = _host(tmp_path, own_id=own_id, has_control=own_id == PC, **kw)
    root._job_start_popup = None
    root.controller.peer_transfer_active = True
    Makera.on_job_start_event(root, _event(not_ready=(DEMO,), seconds_left=30))
    return root, clock, _PopupWithButtons.made[0]


def test_the_starter_is_offered_the_choice_when_the_wait_runs_out_in_a_transfer(tmp_path, app, monkeypatch):
    root, clock, popup = _transfer_host(tmp_path, monkeypatch)
    clock[0] += 10
    Makera._tick_job_start_countdown(root)
    assert "will start in 20 s" in popup.texts[-1]

    clock[0] += 20
    Makera._tick_job_start_countdown(root)
    assert "ran out" in popup.texts[-1]
    assert "Demo" in popup.texts[-1]
    assert popup.keep_waitings[-1] is True
    assert popup.buttons[-1] is True
    assert root._job_start.held

    clock[0] += 300
    Makera._tick_job_start_countdown(root)
    assert root._job_start.held
    assert "ran out" in popup.texts[-1]


def test_a_watcher_is_not_offered_the_choice_in_a_transfer(tmp_path, app, monkeypatch):
    root, clock, popup = _transfer_host(tmp_path, monkeypatch, own_id=THIRD)
    clock[0] += 40
    Makera._tick_job_start_countdown(root)
    assert "ran out" not in popup.texts[-1]
    assert root._job_start.held


def test_keep_waiting_in_a_transfer_restarts_the_countdown_and_is_sent(tmp_path, app, monkeypatch):
    root, clock, popup = _transfer_host(tmp_path, monkeypatch)
    clock[0] += 35
    Makera._tick_job_start_countdown(root)
    popup.on_keep_waiting()
    root.controller.keepWaitingCommand.assert_called_once_with()
    assert "will start in 30 s" in popup.texts[-1]

    clock[0] += 31
    Makera._tick_job_start_countdown(root)
    assert "ran out" in popup.texts[-1]


def test_start_now_in_a_transfer_is_sent_when_the_transfer_ends(tmp_path, app, monkeypatch):
    root, clock, popup = _transfer_host(tmp_path, monkeypatch)
    clock[0] += 35
    Makera._tick_job_start_countdown(root)
    popup.on_start_now()
    root.controller.startNowCommand.assert_not_called()
    assert "waiting for the file transfer" in popup.texts[-1]

    clock[0] += 5
    Makera._tick_job_start_countdown(root)
    root.controller.startNowCommand.assert_not_called()

    root.controller.peer_transfer_active = False
    Makera._tick_job_start_countdown(root)
    root.controller.startNowCommand.assert_called_once_with()
    Makera._tick_job_start_countdown(root)
    root.controller.startNowCommand.assert_called_once_with()


def test_a_queued_start_now_is_dropped_when_the_job_starts_by_itself(tmp_path, app, monkeypatch):
    root, clock, popup = _transfer_host(tmp_path, monkeypatch)
    clock[0] += 35
    Makera._tick_job_start_countdown(root)
    popup.on_start_now()
    Makera.on_job_start_event(
        root, _event(phase=JOB_START_STARTING, reason=JOB_START_REASON_ALL_READY, seconds_left=0, not_ready=())
    )
    root.controller.peer_transfer_active = False
    Makera._tick_job_start_countdown(root)
    root.controller.startNowCommand.assert_not_called()
