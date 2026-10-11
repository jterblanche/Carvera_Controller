"""Another controller's file transfer is not a lost connection.

While the machine runs a WiFi file transfer for one controller, it answers
every other controller with nothing but its busy reply: once for the first
read, then again only for a frame that is not a status query or heartbeat,
and at most every 250 ms (firmware libs/TransferBusy.h). It publishes no
status in that time (WifiProvider::on_idle() returns at once while a
transfer runs). main.py's lost-connection check (blink_state) used to count
only status reports, so a controller watching another one download a file
for more than HEARTBEAT_TIMEOUT closed its link and reconnected.

A transfer over the other link (a USB upload watched over WiFi, or the
reverse) is worse: the machine reads nothing on the other link while it
runs, so the watching controller gets no frame at all, not even the busy
reply. The controller starting a transfer relays a notice first, giving how
long it may take, and another when it ends.

The simulations below drive a real Controller and the real blink_state on a
fake clock, with a stand-in for the firmware's busy-reply rules, so minutes
of transfer run in moments. One test runs the same thing over a real socket.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import carveracontroller.Controller as controller_module
import carveracontroller.machine.peer_transfer as peer_transfer_module
import carveracontroller.main as main_module
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, LOAD_DIR, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.machine.peer_transfer import (
    DEFAULT_ALLOWANCE_S,
    PROBE_INTERVAL_S,
    PeerTransferWatch,
    is_transfer_busy_reply,
    transfer_allowance_s,
)
from carveracontroller.main import Makera
from carveracontroller.protocols.framing import (
    PTYPE_CLIENT_LIST_REPLY,
    PTYPE_CLIENT_LIST_REQ,
    PTYPE_CTRL_MULTI,
    PTYPE_CTRL_SINGLE,
    PTYPE_FILE_START,
    PTYPE_HEARTBEAT,
    PTYPE_LOAD_FINISH,
    PTYPE_NORMAL_INFO,
    PTYPE_RELAY,
    build_frame,
)
from carveracontroller.protocols.relay import (
    decode_transfer_relay,
    encode_transfer_ended_relay,
    encode_transfer_started_relay,
)
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")

BUSY_TEXT = b"error:Busy -- a file transfer is in progress, retry when it finishes\r\n"
# The busy reply exactly as the PC received it on 11 Oct 2026 (firmware
# 6059d61, PC log line 7258), CRC and all.
BUSY_FRAME = b"\x86h\x00I\x90error:Busy -- a file transfer is in progress, retry when it finishes\r\n$HU\xaa"
CLIENT_LIST_FRAME = build_frame(PTYPE_CLIENT_LIST_REPLY, bytes([0]))
PEER_ID = (0x77AE8EB2479D14A7).to_bytes(8, "big")


def _notice_frame(payload):
    """A transfer notice as the machine repeats it: sender id + payload."""
    return build_frame(PTYPE_RELAY, PEER_ID + payload)


# main.py's main() sets this global; tests that never run main() set it too.
HEARTBEAT_TIMEOUT = 5
# blink_state runs every half second (Clock.schedule_interval in main.py).
BLINK_PERIOD_S = 0.5
TICK_S = 0.05


# -- the firmware's side -----------------------------------------------------


class _TransferringMachine:
    """The firmware's busy-reply rules for one client during a transfer
    (BusyReplyLimiter, needs_busy_reply): the first read is answered,
    then a frame other than a status query or heartbeat, at most every
    250 ms. ``dead`` stands in for a link that has gone: nothing comes
    back at all."""

    GAP_S = 0.25

    def __init__(self):
        self.running = True
        self.dead = False
        self._answered_once = False
        self._last_frame_reply = None

    def answer(self, frame, now):
        if self.dead:
            return b""
        if not self.running:
            if frame[4] == PTYPE_CLIENT_LIST_REQ:
                return CLIENT_LIST_FRAME
            return b""
        ptype = frame[4]
        needs_reply = not (ptype == PTYPE_HEARTBEAT or (ptype == PTYPE_CTRL_SINGLE and frame[5:6] == b"?"))
        if not self._answered_once:
            self._answered_once = True
            if needs_reply:
                self._last_frame_reply = now
            return BUSY_FRAME
        if not needs_reply:
            return b""
        if self._last_frame_reply is not None and now - self._last_frame_reply < self.GAP_S:
            return b""
        self._last_frame_reply = now
        return BUSY_FRAME


class _Wire:
    """Stands in for the stream: records every frame the controller sends."""

    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)

    def frames_of_type(self, ptype):
        return [f for f in self.sent if f[4] == ptype]


def _controller_on_wire():
    controller = Controller(CNC(), callback=None, identity=IDENTITY)
    controller.comms.select("makera")
    controller.stream = _Wire()
    return controller


def _receive(controller, data):
    for message in controller.comms.feed(data):
        controller._handle_protocol_message(message)


# -- the app's side: the real lost-connection check --------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0


class _ClockedTime:
    """The time module with time() and monotonic() read from a _Clock.
    Everything else (sleep, ...) is the real module, for any thread that
    still uses it while a test runs."""

    def __init__(self, clock_):
        self._clock = clock_

    def time(self):
        return self._clock.t

    def monotonic(self):
        return self._clock.t

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    # Only main.py's and Controller.py's own references to the time module
    # are replaced, so nothing else in the process sees the fake clock.
    monkeypatch.setattr(main_module, "time", _ClockedTime(c))
    monkeypatch.setattr(controller_module, "time", _ClockedTime(c))
    monkeypatch.setattr(main_module, "HEARTBEAT_TIMEOUT", HEARTBEAT_TIMEOUT, raising=False)
    monkeypatch.setattr(main_module.App, "get_running_app", lambda: None)
    return c


def _app_root(controller, now):
    """A Makera holding just what blink_state reads."""
    root = Makera.__new__(Makera)
    root.uploading = False
    root.downloading = False
    root.holding = root.pausing = root.waiting = root.tooling = 0
    root.status_data_view = SimpleNamespace(color=None)
    root.controller = controller
    root.heartbeat_time = now
    root.file_just_loaded = False
    root.status_drop_down = SimpleNamespace(set_connected_controllers=MagicMock())
    root.updateStatus = MagicMock()
    # Spy on the close; a manual-disconnect flag keeps the reconnect popup out.
    controller.close = MagicMock()
    controller._manual_disconnect = True
    return root


def _run(controller, root, machine, clock, seconds):
    """Run streamIO's heartbeat step and the machine's answers every tick,
    and blink_state every half second, for `seconds` of fake time. Returns
    the fake time at which blink_state closed the link, or None."""
    end = clock.t + seconds
    next_blink = clock.t + BLINK_PERIOD_S
    while clock.t < end:
        clock.t += TICK_S
        before = len(controller.stream.sent)
        controller._advance_heartbeat(clock.t)
        for frame in controller.stream.sent[before:]:
            reply = machine.answer(frame, clock.t)
            if reply:
                _receive(controller, reply)
        if clock.t >= next_blink:
            next_blink += BLINK_PERIOD_S
            root.blink_state()
            if controller.close.called:
                return clock.t
    return None


# -- the busy reply itself ---------------------------------------------------


def test_the_logged_busy_frame_is_the_firmware_reply():
    assert build_frame(PTYPE_NORMAL_INFO, BUSY_TEXT) == BUSY_FRAME


def test_busy_reply_recognised_and_nothing_else():
    assert is_transfer_busy_reply(BUSY_TEXT.decode())
    assert is_transfer_busy_reply("  ERROR:busy -- a file transfer is in progress  ")
    assert not is_transfer_busy_reply("error:Refused -- PC has control")
    assert not is_transfer_busy_reply("error: Machine is busy.")
    assert not is_transfer_busy_reply("")


def test_watch_shows_the_first_reply_and_ones_answering_a_command():
    watch = PeerTransferWatch()
    assert watch.on_busy_reply() is True
    assert watch.on_busy_reply() is False  # answers a probe
    watch.note_command_sent()
    assert watch.on_busy_reply() is True
    assert watch.on_busy_reply() is False
    assert watch.on_other_frame() is True
    assert watch.active is False
    assert watch.on_other_frame() is False


def test_watch_probes_once_per_interval_only_while_a_transfer_runs():
    watch = PeerTransferWatch()
    assert watch.probe_due(10.0) is False
    watch.on_busy_reply()
    assert watch.probe_due(10.0) is True
    watch.probe_sent(10.0)
    assert watch.probe_due(10.0 + PROBE_INTERVAL_S - 0.01) is False
    assert watch.probe_due(10.0 + PROBE_INTERVAL_S) is True
    watch.on_other_frame()
    assert watch.probe_due(100.0) is False


def test_probes_prove_the_link_well_inside_the_timeout():
    # Each probe is answered (the firmware allows one answer per 250 ms),
    # so several fit inside the time the app allows before it gives up.
    assert PROBE_INTERVAL_S >= _TransferringMachine.GAP_S
    assert PROBE_INTERVAL_S * 4 <= HEARTBEAT_TIMEOUT


# -- the connection --------------------------------------------------------------


def test_link_kept_through_a_ten_minute_transfer_and_free_after(clock):
    """The case from the 11 Oct checkpoint: the PC's heartbeat got the busy
    reply, then nothing. Ten minutes is far longer than any transfer seen
    so far (the 5 MB test file went as a 153 KB compressed download)."""
    controller = _controller_on_wire()
    root = _app_root(controller, clock.t)
    machine = _TransferringMachine()

    _receive(controller, machine.answer(build_frame(PTYPE_HEARTBEAT), clock.t))
    assert controller.peer_transfer_active

    assert _run(controller, root, machine, clock, seconds=600) is None
    controller.close.assert_not_called()
    probes = controller.stream.frames_of_type(PTYPE_CLIENT_LIST_REQ)
    assert len(probes) >= 500  # about one a second (ticks are 50 ms)

    machine.running = False
    assert _run(controller, root, machine, clock, seconds=3) is None
    assert not controller.peer_transfer_active
    sent = len(controller.stream.sent)
    _run(controller, root, machine, clock, seconds=5)
    assert len(controller.stream.sent) == sent  # no probes once it has ended


def test_total_silence_during_a_transfer_still_loses_the_link(clock):
    controller = _controller_on_wire()
    root = _app_root(controller, clock.t)
    machine = _TransferringMachine()

    _receive(controller, machine.answer(build_frame(PTYPE_HEARTBEAT), clock.t))
    assert _run(controller, root, machine, clock, seconds=20) is None

    machine.dead = True
    gone_at = clock.t
    lost_at = _run(controller, root, machine, clock, seconds=20)
    assert lost_at is not None
    # The last answer came at most one probe before the link went.
    assert HEARTBEAT_TIMEOUT - PROBE_INTERVAL_S < lost_at - gone_at <= HEARTBEAT_TIMEOUT + 2 * BLINK_PERIOD_S


def test_total_silence_without_any_transfer_still_loses_the_link(clock):
    controller = _controller_on_wire()
    root = _app_root(controller, clock.t)
    machine = _TransferringMachine()
    machine.dead = True

    lost_at = _run(controller, root, machine, clock, seconds=20)
    assert lost_at is not None
    assert not controller.stream.sent  # not subscribed, no transfer: nothing sent


# -- this controller's own requests -----------------------------------------------


def test_listing_turned_away_is_sent_again_when_the_transfer_ends():
    controller = _controller_on_wire()
    controller.loadNUM = LOAD_DIR
    controller.loadEOF = controller.loadERR = False
    controller.lsCommand("/sd/gcodes")
    (listing,) = controller.stream.frames_of_type(PTYPE_CTRL_MULTI)

    _receive(controller, BUSY_FRAME)
    assert controller.peer_transfer_active
    assert controller.load_buffer.qsize() == 0  # not taken for listing data
    assert controller.loadERR is False
    assert controller.stream.frames_of_type(PTYPE_CTRL_MULTI) == [listing]

    _receive(controller, CLIENT_LIST_FRAME)
    assert not controller.peer_transfer_active
    assert controller.stream.frames_of_type(PTYPE_CTRL_MULTI) == [listing, listing]


def test_finished_listing_is_not_sent_again():
    controller = _controller_on_wire()
    controller.loadNUM = 0
    controller.lsCommand("/sd/gcodes")
    _receive(controller, BUSY_FRAME)
    _receive(controller, CLIENT_LIST_FRAME)
    assert len(controller.stream.frames_of_type(PTYPE_CTRL_MULTI)) == 1


def _console(controller):
    lines = []
    while not controller.log.empty():
        lines.append(controller.log.get_nowait()[1])
    return lines


def test_console_shows_the_reply_once_and_again_for_a_command():
    controller = _controller_on_wire()
    _console(controller)  # the protocol selection's own line
    _receive(controller, BUSY_FRAME)
    _receive(controller, BUSY_FRAME)  # answers a probe
    _receive(controller, BUSY_FRAME)
    assert _console(controller) == [BUSY_TEXT.decode().strip()]

    controller.executeCommand("version")
    _receive(controller, BUSY_FRAME)
    _receive(controller, BUSY_FRAME)
    assert _console(controller) == [BUSY_TEXT.decode().strip()]


def test_any_frame_counts_as_the_link_being_alive():
    controller = _controller_on_wire()
    controller._refresh_heartbeat = False
    _receive(controller, BUSY_FRAME)
    assert controller._refresh_heartbeat is True
    controller._refresh_heartbeat = False
    _receive(controller, CLIENT_LIST_FRAME)
    assert controller._refresh_heartbeat is True


def test_closing_the_link_ends_a_transfer():
    controller = _controller_on_wire()
    notified = []
    controller._notify_peer_transfer_finished = lambda: notified.append(True)
    _receive(controller, BUSY_FRAME)
    controller._reset_pending_sends()
    assert not controller.peer_transfer_active
    assert notified == [True]


def _held_actions_host(peer_transfer_active):
    root = Makera.__new__(Makera)
    root._held_for_passive_fetch_lock = threading.Lock()
    root._held_for_passive_fetch = ()
    root._auto_fetch_in_progress = False
    root._passive_fetch_hint_shown = False
    root.backing_up_config = False
    root.progressStart = MagicMock()
    root.progressFinish = MagicMock()
    root._link_workers = 0
    root.uploading = root.downloading = False
    root.decompstatus = False
    root.controller = SimpleNamespace(
        peer_transfer_active=peer_transfer_active,
        stream=object(),
        sendNUM=0,
        loadNUM=0,
        log=SimpleNamespace(put=MagicMock()),
    )
    return root


@pytest.fixture
def run_clock_now(monkeypatch):
    monkeypatch.setattr(main_module.Clock, "schedule_once", lambda fn, *_a, **_kw: fn(0))


def test_operator_action_waits_for_the_transfer_then_runs(run_clock_now):
    root = _held_actions_host(peer_transfer_active=True)
    action = MagicMock()

    assert root._hold_for_passive_fetch(action) is True
    action.assert_not_called()
    root.progressStart.assert_called_once()
    assert "another controller" in root.progressStart.call_args[0][0]

    root.controller.peer_transfer_active = False
    root.on_peer_transfer_finished()
    action.assert_called_once_with()
    root.progressFinish.assert_called_once()


def test_operator_action_goes_at_once_without_a_transfer(run_clock_now):
    root = _held_actions_host(peer_transfer_active=False)
    assert root._hold_for_passive_fetch(MagicMock()) is False
    root.progressStart.assert_not_called()


def test_held_actions_dropped_when_the_link_closes_during_the_transfer(run_clock_now):
    root = _held_actions_host(peer_transfer_active=True)
    action = MagicMock()
    root._hold_for_passive_fetch(action)

    root.controller.peer_transfer_active = False
    root.controller.stream = None
    root.on_peer_transfer_finished()
    action.assert_not_called()
    assert root._held_for_passive_fetch == ()


def test_no_fetch_of_ours_starts_during_the_transfer():
    root = _held_actions_host(peer_transfer_active=True)
    assert root._link_busy_for_passive_fetch() is True
    root.controller.peer_transfer_active = False
    assert root._link_busy_for_passive_fetch() is False


# -- over a real socket ------------------------------------------------------------


class _TransferHook:
    """A FakeMachine frame hook playing the machine during another
    controller's transfer, until end() is called."""

    def __init__(self):
        self.machine_rules = _TransferringMachine()
        self.lock = threading.Lock()

    def end(self):
        with self.lock:
            self.machine_rules.running = False

    def __call__(self, conn, ptype, payload):
        with self.lock:
            running = self.machine_rules.running
            reply = b""
            if running:
                frame = build_frame(ptype, payload)
                reply = self.machine_rules.answer(frame, time.monotonic())
        if running:
            if reply:
                conn.sendall(reply)
            return True
        if ptype == PTYPE_CTRL_MULTI and payload.startswith(b"ls "):
            conn.sendall(build_frame(PTYPE_LOAD_FINISH))
            return True
        return False


def test_over_a_socket_probes_keep_coming_and_the_listing_goes_again(monkeypatch):
    monkeypatch.setattr(peer_transfer_module, "PROBE_INTERVAL_S", 0.3)
    hook = _TransferHook()
    machine = FakeMachine(mode="new", frame_hook=None)
    controller = Controller(CNC(), callback=None, identity=IDENTITY)
    try:
        controller.open(CONN_WIFI, machine.address())
        assert machine.wait_until(lambda: controller._hello is not None and controller._hello.identified, 3.0)
        machine.frame_hook = hook

        controller.loadNUM = LOAD_DIR
        controller.loadEOF = controller.loadERR = False
        controller.lsCommand("/sd/gcodes")
        assert machine.wait_until(lambda: controller.peer_transfer_active, 2.0)

        probes_before = len(machine.frames_of_type(PTYPE_CLIENT_LIST_REQ))
        time.sleep(1.5)
        assert len(machine.frames_of_type(PTYPE_CLIENT_LIST_REQ)) - probes_before >= 3
        assert controller.stream is not None

        hook.end()
        assert machine.wait_until(lambda: not controller.peer_transfer_active, 2.0)
        assert machine.wait_until(lambda: controller.loadEOF, 2.0)
        listings = [p for p in machine.frames_of_type(PTYPE_CTRL_MULTI) if p.startswith(b"ls ")]
        assert len(listings) == 2
    finally:
        controller.close(allow_reconnect=False)
        machine.stop()


# -- a transfer over the other link: notices ---------------------------------------


def test_notice_payloads_round_trip():
    assert decode_transfer_relay(encode_transfer_started_relay(1078)) == ("started", 1078)
    assert decode_transfer_relay(encode_transfer_ended_relay()) == ("ended", 0)
    assert decode_transfer_relay(encode_transfer_started_relay(10**9)) == ("started", 0xFFFF)
    assert decode_transfer_relay(bytes([1, 0])) is None  # a tool table
    assert decode_transfer_relay(bytes([2, 0])) is None  # cut short
    assert decode_transfer_relay(b"") is None


def test_allowance_grows_with_size_and_has_a_default():
    assert transfer_allowance_s(None) == DEFAULT_ALLOWANCE_S
    assert transfer_allowance_s(0) == 30
    # The 1 MB test file went as about 31 KB compressed.
    assert transfer_allowance_s(31_000) == 61
    assert transfer_allowance_s(5_242_287) == 30 + 5242
    assert transfer_allowance_s(10**12) == 0xFFFF


def _identified(controller):
    controller._hello = SimpleNamespace(identified=True, resolved=True)
    return controller


def test_own_upload_is_announced_before_it_starts_and_ended_after():
    controller = _identified(_controller_on_wire())
    controller.pauseStream(0.0)
    assert controller.uploadCommand("/sd/gcodes/job.nc", size=31_000)
    controller.resumeStream()

    kinds = [f[4] for f in controller.stream.sent]
    assert kinds == [PTYPE_RELAY, PTYPE_FILE_START, PTYPE_RELAY]
    started, _, ended = controller.stream.sent
    assert decode_transfer_relay(started[5:-4]) == ("started", 61)
    assert decode_transfer_relay(ended[5:-4]) == ("ended", 0)

    controller.resumeStream()  # a second resume (an error path) sends nothing more
    assert len(controller.stream.sent) == 3


def test_own_download_is_announced_with_the_default_allowance():
    controller = _identified(_controller_on_wire())
    assert controller.downloadCommand("/sd/gcodes/job.nc")
    assert decode_transfer_relay(controller.stream.sent[0][5:-4]) == ("started", DEFAULT_ALLOWANCE_S)


def test_nothing_announced_before_identifying():
    controller = _controller_on_wire()
    assert controller.uploadCommand("/sd/gcodes/job.nc", size=31_000)
    controller.resumeStream()
    assert [f[4] for f in controller.stream.sent] == [PTYPE_FILE_START]


def test_silent_machine_during_an_announced_upload_keeps_the_link(clock):
    """The 11 Oct case: PC uploaded over USB, and Demo on WiFi got no frame
    at all and dropped. Five minutes of total silence inside the notice
    (1 MB uncompressible over USB would be about 95 s at 11 KB/s)."""
    controller = _controller_on_wire()
    root = _app_root(controller, clock.t)
    machine = _TransferringMachine()
    machine.dead = True  # nothing at all comes back during the transfer
    finished = []
    controller._notify_peer_transfer_finished = lambda: finished.append(clock.t)

    _receive(controller, _notice_frame(encode_transfer_started_relay(transfer_allowance_s(1_048_767))))
    assert controller.peer_transfer_active
    assert controller.stream.sent == []  # notices alone send no probes

    assert _run(controller, root, machine, clock, seconds=300) is None

    _receive(controller, _notice_frame(encode_transfer_ended_relay()))
    assert not controller.peer_transfer_active
    assert len(finished) == 1
    ended_at = clock.t
    lost_at = _run(controller, root, machine, clock, seconds=20)
    assert lost_at is not None  # and silence after it is a lost link again
    # The end notice is itself a frame; then the usual timeout, checked
    # every half second.
    assert lost_at - ended_at <= HEARTBEAT_TIMEOUT + 3 * BLINK_PERIOD_S


def test_a_notice_that_runs_out_no_longer_covers_silence(clock):
    controller = _controller_on_wire()
    root = _app_root(controller, clock.t)
    machine = _TransferringMachine()
    machine.dead = True
    finished = []
    controller._notify_peer_transfer_finished = lambda: finished.append(clock.t)

    _receive(controller, _notice_frame(encode_transfer_started_relay(60)))
    started_at = clock.t
    lost_at = _run(controller, root, machine, clock, seconds=120)
    assert lost_at is not None
    assert 60 < lost_at - started_at <= 60 + HEARTBEAT_TIMEOUT + 2 * BLINK_PERIOD_S
    assert len(finished) == 1


def test_notice_counts_as_a_running_transfer_until_its_time(clock):
    controller = _controller_on_wire()
    _receive(controller, _notice_frame(encode_transfer_started_relay(120)))
    assert controller.peer_transfer_active
    clock.t += 121
    assert not controller.peer_transfer_active


def test_tool_table_relay_is_not_a_notice():
    controller = _controller_on_wire()
    controller._notify_relayed_tool_table = lambda table: None
    _receive(controller, _notice_frame(bytes([1, 1, 0, 3, 3]) + b"T3a"))
    assert not controller.peer_transfer_active
    assert controller.relayed_tool_table == {3: "T3a"}
