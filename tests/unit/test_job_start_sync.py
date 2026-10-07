"""The controller's side of the job-start sync.

A machine that holds starts announces a job with a job-start event (0x68
kind 8) and waits, up to its time limit, until every other controller that
takes part has drawn the job's file and sent ready (0x6C). These tests
drive the controller through that from the events: drawing a local copy
at once, fetching a file it lacks before the start, reporting ready (again,
when the machine still lists it), the countdown on every controller with
Start now and Cancel on the starter only, a cancelled start, a start at the
time limit, resume-at-line, and firmware without the hold.
"""

from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller import Utils
from carveracontroller.CNC import CNC
from carveracontroller.Controller import Controller
from carveracontroller.machine.control_refusal import JOB_START_REFUSAL
from carveracontroller.machine.hello import HelloNegotiator
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.machine.job_start import JobStartTracker
from carveracontroller.machine.local_copies import LocalCopyStore
from carveracontroller.machine.passive_fetch import PassiveFetchTracker
from carveracontroller.main import Makera
from carveracontroller.protocols.framing import build_frame
from carveracontroller.protocols.handshake import (
    EVENT_KIND_PLAY_STARTED,
    HELLO_ACCEPTED,
    JOB_START_CANCELLED,
    JOB_START_REASON_ABORT,
    JOB_START_REASON_ALL_READY,
    JOB_START_REASON_HALT,
    JOB_START_REASON_START_NOW,
    JOB_START_REASON_STARTER_LEFT,
    JOB_START_REASON_TIME_LIMIT,
    JOB_START_REASON_WAITING,
    JOB_START_STARTING,
    JOB_START_WAITING,
    ClientEntry,
    JobStartEvent,
)
from carveracontroller.protocols.makera import HELLO_PROTOCOL_VERSION
from carveracontroller.protocols.messages import MessageKind, ParsedMessage

PC = 0x1111  # the controller that starts the job
DEMO = 0x2222  # the controller under test, unless it is the starter
THIRD = 0x3333

PATH = "/sd/gcodes/air-test-long.nc"
CONTENT = b"G21\nG90\nG0 X0 Y0\nG1 X10 F500\n"
MD5 = hashlib.md5(CONTENT).digest()
SIZE = len(CONTENT)


def _event(
    start_id=7,
    phase=JOB_START_WAITING,
    reason=JOB_START_REASON_WAITING,
    seconds_left=30,
    starter_id=PC,
    not_ready=(DEMO,),
    path=PATH,
    size=SIZE,
    checksum=MD5,
):
    return JobStartEvent(
        path=path,
        size=size,
        checksum=checksum,
        start_id=start_id,
        phase=phase,
        reason=reason,
        seconds_left=seconds_left,
        starter_id=starter_id,
        not_ready_ids=tuple(not_ready),
    )


class _FakePopup:
    def __init__(self):
        self.showing = False
        self.texts = []
        self.buttons = []

    def update(self, text, show_buttons):
        self.showing = True
        self.texts.append(text)
        self.buttons.append(show_buttons)

    def close(self):
        self.showing = False


class _HeldThread:
    """Records each thread started and runs it only when the test says so,
    with its keyword arguments."""

    started: list = []

    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        _HeldThread.started.append(self)

    def run(self):
        self.target(*self.args, **self.kwargs)


class _ImmediateThread(_HeldThread):
    def start(self):
        self.run()


@pytest.fixture
def app(monkeypatch):
    """The running app on firmware that reports the player flag, machine
    idle and not playing, another file on screen."""
    running = SimpleNamespace(
        selected_remote_filename="/sd/gcodes/spindle-test.nc",
        selected_local_filename="/tmp/spindle-test.nc",
        state="Idle",
        playing=False,
        is_community_firmware=True,
        fw_version_digitized=Utils.digitize_v("2.2.0"),
    )
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: running)
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: cb(0))
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_interval", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    monkeypatch.setitem(CNC.vars, "is_playing", 0)
    return running


def _host(tmp_path, own_id=DEMO, has_control=False):
    root = Makera.__new__(Makera)
    root.temp_dir = str(tmp_path / "cache")
    os.makedirs(root.temp_dir, exist_ok=True)
    root.identity = SimpleNamespace(id=own_id)
    root._passive_fetch = PassiveFetchTracker()
    root._auto_fetch_in_progress = False
    root._local_copies = LocalCopyStore()
    root._job_start = JobStartTracker(own_id=own_id)
    root._job_start_popup = _FakePopup()
    root._job_start_clock = None
    root.controller = SimpleNamespace(
        has_control=has_control,
        sendNUM=0,
        loadNUM=0,
        connected_clients=(
            ClientEntry(id=PC, name="PC", link=1, has_control=True),
            ClientEntry(id=DEMO, name="Demo", link=0, has_control=False),
            ClientEntry(id=THIRD, name="Shop", link=0, has_control=False),
        ),
        send_job_start_ready=MagicMock(return_value=True),
        startNowCommand=MagicMock(),
        abortCommand=MagicMock(),
        log=MagicMock(),
    )

    def download(remote_path, local_path, show_progress=True, open_after=True, automatic=False):
        with open(local_path, "wb") as f:
            f.write(CONTENT)
        return 1

    root.doDownload = MagicMock(side_effect=download)
    root.load_gcode_file = MagicMock()
    root.show_message_popup = MagicMock()
    root.clear_selection = MagicMock()
    root._last_loaded_file_key = None
    root._job_start_late = None
    root._job_without_toolpath = None
    return root


def _local_copy(tmp_path, name="air-test-long.nc", content=CONTENT):
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def _logged(root):
    return [c.args[0][1] for c in root.controller.log.put.call_args_list]


# -- a local copy with the same content ---------------------------------------


def test_identical_copy_of_a_card_file_is_drawn_without_a_fetch(tmp_path, app):
    """Another controller plays a file from the card. This controller
    fetched an identical copy earlier and has a different file on screen. It draws its copy at once, fetches nothing,
    and reports ready."""
    root = _host(tmp_path)
    copy = _local_copy(tmp_path)
    root._local_copies.remember(copy)

    Makera.on_job_start_event(root, _event())

    root.doDownload.assert_not_called()
    root.load_gcode_file.assert_called_once_with(copy)
    assert app.selected_remote_filename == PATH
    assert app.selected_local_filename == copy
    root.controller.send_job_start_ready.assert_called_once_with(7)

    # The job starts; play-started carries the same size and MD5.
    Makera.on_job_start_event(root, _event(phase=JOB_START_STARTING, reason=JOB_START_REASON_ALL_READY, not_ready=()))
    Makera.on_passive_file_published(root, PATH, MD5, size=len(CONTENT), played=True)

    root.doDownload.assert_not_called()
    root.load_gcode_file.assert_called_once()
    assert app.selected_remote_filename == PATH
    assert app.selected_local_filename == copy
    assert root._passive_fetch.pending_path is None


def test_copy_already_on_screen_reports_ready_at_once(tmp_path, app):
    root = _host(tmp_path)
    copy = _local_copy(tmp_path, "renamed.nc")
    app.selected_remote_filename = "/sd/gcodes/renamed.nc"
    app.selected_local_filename = copy

    Makera.on_job_start_event(root, _event())

    root.doDownload.assert_not_called()
    root.load_gcode_file.assert_not_called()
    assert app.selected_remote_filename == PATH  # named as the machine names it
    root.controller.send_job_start_ready.assert_called_once_with(7)


def test_play_started_with_a_matching_copy_draws_it_at_once(tmp_path, app):
    """Without a hold (the machine's wait is turned off, or this controller
    was not waited for), play-started's size and MD5 still let a controller
    holding an identical copy draw it at once instead of after the job."""
    root = _host(tmp_path)
    copy = _local_copy(tmp_path)
    root._local_copies.remember(copy)

    Makera.on_passive_file_published(root, PATH, MD5, size=len(CONTENT), played=True)

    root.doDownload.assert_not_called()
    root.load_gcode_file.assert_called_once_with(copy)
    assert app.selected_local_filename == copy
    assert root._passive_fetch.pending_path is None


def test_play_started_without_a_copy_shows_the_job_without_its_toolpath(tmp_path, app):
    """No local copy: the other file's drawing is removed, the job's name
    and progress are shown with a plain note that the toolpath is not
    loaded, and the fetch waits until the player stops."""
    root = _host(tmp_path)
    CNC.vars["is_playing"] = 1

    Makera.on_passive_file_published(root, PATH, MD5, size=len(CONTENT), played=True)

    root.doDownload.assert_not_called()
    assert app.selected_remote_filename == ""
    assert app.selected_local_filename == ""
    root.clear_selection.assert_called_once()
    assert root._job_without_toolpath == PATH
    assert any("toolpath is not shown" in text for text in _logged(root))
    assert root._passive_fetch.pending_path == PATH


def test_a_copy_with_the_same_name_but_other_content_is_not_drawn(tmp_path, app):
    root = _host(tmp_path)
    root._local_copies.remember(_local_copy(tmp_path, content=b"G0 X99\n"))
    CNC.vars["is_playing"] = 1

    Makera.on_passive_file_published(root, PATH, MD5, size=len(CONTENT), played=True)

    root.load_gcode_file.assert_not_called()
    assert app.selected_remote_filename == ""


def test_old_play_started_without_size_or_checksum_behaves_as_before(tmp_path, app):
    """Firmware without the hold names only the path: even an identical
    local copy is not drawn from it, and the file is fetched once the job
    has ended, exactly as before."""
    root = _host(tmp_path)
    root._local_copies.remember(_local_copy(tmp_path))
    CNC.vars["is_playing"] = 1

    Makera.on_passive_file_published(root, PATH, b"", size=None, played=True)

    root.load_gcode_file.assert_not_called()
    root.doDownload.assert_not_called()
    assert app.selected_remote_filename == ""
    assert root._passive_fetch.pending_path == PATH


def test_play_started_is_never_taken_for_own_upload(tmp_path, app):
    """This controller uploaded the file earlier; another controller now
    plays it while a different file is on screen. That file must not stay
    under the job's progress."""
    root = _host(tmp_path)
    root._passive_fetch.note_own_upload(PATH, MD5.hex())
    CNC.vars["is_playing"] = 1

    Makera.on_passive_file_published(root, PATH, MD5, size=len(CONTENT), played=True)

    assert app.selected_remote_filename == ""
    assert root._passive_fetch.pending_path == PATH


# -- fetching before the start ------------------------------------------------


def test_card_file_this_controller_lacks_is_fetched_before_the_start(tmp_path, app, monkeypatch):
    """A file played from the card that this controller has never had: it
    is fetched while the machine holds the start, drawn, and ready follows.
    A waiting event during the fetch starts nothing more."""
    _HeldThread.started = []
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _HeldThread)
    root = _host(tmp_path)

    Makera.on_job_start_event(root, _event())
    assert app.selected_remote_filename == ""  # the old drawing is gone at once
    assert len(_HeldThread.started) == 1
    fetch = _HeldThread.started[0]
    assert fetch.args == (PATH,)

    Makera.on_job_start_event(root, _event(seconds_left=29))
    assert len(_HeldThread.started) == 1
    root.controller.send_job_start_ready.assert_not_called()

    fetch.run()  # the download; the drawing is the next thread
    root.doDownload.assert_called_once()
    assert root.doDownload.call_args.kwargs["automatic"] is True
    local = os.path.join(root.temp_dir, "air-test-long.nc")
    assert app.selected_remote_filename == PATH
    assert app.selected_local_filename == local
    _HeldThread.started[1].run()

    root.load_gcode_file.assert_called_once_with(local)
    root.controller.send_job_start_ready.assert_called_once_with(7)
    assert root._auto_fetch_in_progress is False
    assert root._passive_fetch.pending_path is None


def test_ready_is_sent_again_while_the_machine_still_lists_this_controller(tmp_path, app):
    root = _host(tmp_path)
    root._local_copies.remember(_local_copy(tmp_path))

    Makera.on_job_start_event(root, _event())
    Makera.on_job_start_event(root, _event(seconds_left=29))
    Makera.on_job_start_event(root, _event(seconds_left=28, not_ready=()))

    assert root.controller.send_job_start_ready.call_count == 2
    root.load_gcode_file.assert_called_once()


def _download_results(*results):
    """A doDownload stand-in: each call writes the next content (None: the
    download fails)."""
    calls = list(results)

    def download(remote_path, local_path, show_progress=True, open_after=True, automatic=False):
        content = calls.pop(0) if calls else results[-1]
        if content is None:
            return None
        with open(local_path, "wb") as f:
            f.write(content)
        return 1

    return MagicMock(side_effect=download)


def _clock(monkeypatch, start=100.0):
    now = [start]
    monkeypatch.setattr("carveracontroller.main.time.monotonic", lambda: now[0])
    return now


def _hold_until(root, now, first=30, last=1, **kw):
    """Waiting events once a second, `first` down to `last` seconds left."""
    start = now[0]
    for left in range(first, last - 1, -1):
        now[0] = start + (first - left)
        Makera.on_job_start_event(root, _event(seconds_left=left, **kw))


def test_a_failed_download_is_retried_and_ready_follows_a_good_copy(tmp_path, app, monkeypatch):
    now = _clock(monkeypatch)
    root = _host(tmp_path)
    root.doDownload = _download_results(None, CONTENT)

    Makera.on_job_start_event(root, _event(seconds_left=30))
    root.controller.send_job_start_ready.assert_not_called()
    assert root.doDownload.call_count == 1

    now[0] += 1  # still backing off
    Makera.on_job_start_event(root, _event(seconds_left=29))
    assert root.doDownload.call_count == 1

    now[0] += 1
    Makera.on_job_start_event(root, _event(seconds_left=28))
    assert root.doDownload.call_count == 2
    root.controller.send_job_start_ready.assert_called_once_with(7)
    assert app.selected_remote_filename == PATH


def test_a_download_failing_until_the_time_limit_never_sends_ready(tmp_path, app, monkeypatch):
    """Retries back off and stop when too little time is left. The job
    starts at the limit: its name and progress are shown with the
    toolpath-not-loaded note, nothing is plotted, and the file is fetched
    once the job has ended."""
    now = _clock(monkeypatch)
    root = _host(tmp_path)
    root.doDownload = _download_results(None)

    _hold_until(root, now)
    attempts = root.doDownload.call_count
    assert 1 < attempts < 10

    Makera.on_job_start_event(
        root, _event(phase=JOB_START_STARTING, reason=JOB_START_REASON_TIME_LIMIT, seconds_left=0)
    )
    root.controller.send_job_start_ready.assert_not_called()
    assert root._job_without_toolpath == PATH
    assert app.selected_remote_filename == ""
    assert app.selected_local_filename == ""
    assert any("toolpath is not shown" in text for text in _logged(root))

    # The job plays: nothing is fetched.
    CNC.vars["is_playing"] = 1
    Makera.on_passive_file_published(root, PATH, MD5, size=len(CONTENT), played=True)
    for _ in range(3):
        Makera._check_passive_fetch(root, True)
    assert root.doDownload.call_count == attempts

    # The job ends: the file is fetched and drawn.
    root.doDownload = _download_results(CONTENT)
    CNC.vars["is_playing"] = 0
    now[0] += 60
    for _ in range(3):
        Makera._check_passive_fetch(root, True)
    root.doDownload.assert_called_once()
    assert app.selected_remote_filename == PATH
    assert root._job_without_toolpath is None
    root.controller.send_job_start_ready.assert_not_called()


def test_no_retry_starts_with_too_little_time_left(tmp_path, app, monkeypatch):
    now = _clock(monkeypatch)
    root = _host(tmp_path)
    root.doDownload = _download_results(None)

    Makera.on_job_start_event(root, _event(seconds_left=4))
    now[0] += 10
    Makera.on_job_start_event(root, _event(seconds_left=2))

    assert root.doDownload.call_count == 1


def test_a_download_that_is_not_the_announced_file_sends_no_ready(tmp_path, app, monkeypatch):
    """The card's MD5 does not match what was downloaded: the drawing is
    removed, no ready is sent, and a later attempt is made."""
    now = _clock(monkeypatch)
    root = _host(tmp_path)
    root.doDownload = _download_results(b"G0 X99 Y99\n")

    Makera.on_job_start_event(root, _event())

    root.load_gcode_file.assert_called_once()
    root.controller.send_job_start_ready.assert_not_called()
    assert app.selected_remote_filename == ""
    root.clear_selection.assert_called()

    now[0] += 2
    Makera.on_job_start_event(root, _event(seconds_left=28))
    assert root.doDownload.call_count == 2
    root.controller.send_job_start_ready.assert_not_called()


def test_a_local_copy_that_fails_to_draw_sends_no_ready(tmp_path, app):
    root = _host(tmp_path)
    root._local_copies.remember(_local_copy(tmp_path))
    root.load_gcode_file = MagicMock(side_effect=ValueError("bad"))

    Makera.on_job_start_event(root, _event())

    root.controller.send_job_start_ready.assert_not_called()


def test_without_an_announced_md5_nothing_is_fetched_and_no_ready_is_sent(tmp_path, app, monkeypatch):
    """No copy can be checked, so none is fetched during the hold. The job
    is shown without its toolpath and fetched after it."""
    now = _clock(monkeypatch)
    root = _host(tmp_path)

    _hold_until(root, now, first=5, checksum=b"")
    Makera.on_job_start_event(
        root,
        _event(phase=JOB_START_STARTING, reason=JOB_START_REASON_TIME_LIMIT, seconds_left=0, checksum=b""),
    )

    root.doDownload.assert_not_called()
    root.controller.send_job_start_ready.assert_not_called()
    assert root._job_without_toolpath == PATH
    assert root._passive_fetch.pending_path == PATH


def test_never_fetches_while_the_machine_reports_a_file_playing(tmp_path, app, monkeypatch):
    _HeldThread.started = []
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _HeldThread)
    root = _host(tmp_path)
    CNC.vars["is_playing"] = 1

    Makera.on_job_start_event(root, _event())

    assert _HeldThread.started == []
    root.controller.send_job_start_ready.assert_not_called()


def test_waits_for_its_own_transfer_then_fetches_on_the_next_event(tmp_path, app, monkeypatch):
    _HeldThread.started = []
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _HeldThread)
    root = _host(tmp_path)
    root.controller.loadNUM = 1  # a listing is waiting for its reply

    Makera.on_job_start_event(root, _event())
    assert _HeldThread.started == []

    root.controller.loadNUM = 0
    Makera.on_job_start_event(root, _event(seconds_left=29))
    assert len(_HeldThread.started) == 1


def test_an_event_that_does_not_list_this_controller_asks_nothing(tmp_path, app):
    root = _host(tmp_path)

    Makera.on_job_start_event(root, _event(not_ready=(THIRD,)))

    root.doDownload.assert_not_called()
    root.controller.send_job_start_ready.assert_not_called()
    assert root._job_start_popup.showing


# -- countdown, Start now, Cancel ---------------------------------------------


def test_every_controller_shows_the_countdown_without_buttons(tmp_path, app):
    root = _host(tmp_path)

    Makera.on_job_start_event(root, _event(not_ready=(THIRD,)))

    popup = root._job_start_popup
    assert popup.showing
    assert popup.buttons == [False]
    assert "air-test-long.nc" in popup.texts[-1]
    assert "30" in popup.texts[-1]
    assert "Shop" in popup.texts[-1]


def test_the_starter_gets_start_now_and_cancel(tmp_path, app):
    root = _host(tmp_path, own_id=PC, has_control=True)

    Makera.on_job_start_event(root, _event(not_ready=(DEMO,)))

    popup = root._job_start_popup
    assert popup.buttons == [True]
    assert "Demo" in popup.texts[-1]
    root.controller.send_job_start_ready.assert_not_called()
    root.doDownload.assert_not_called()


def test_countdown_counts_down_between_events(tmp_path, app, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("carveracontroller.main.time.monotonic", lambda: clock[0])
    root = _host(tmp_path)

    Makera.on_job_start_event(root, _event(not_ready=(THIRD,)))
    clock[0] += 3
    Makera._tick_job_start_countdown(root)

    assert "27" in root._job_start_popup.texts[-1]


def test_countdown_closes_once_the_job_is_playing(tmp_path, app):
    """The starting event can be lost; the playing flag ends the countdown."""
    root = _host(tmp_path)
    Makera.on_job_start_event(root, _event(not_ready=(THIRD,)))

    CNC.vars["is_playing"] = 1
    Makera._tick_job_start_countdown(root)

    assert not root._job_start_popup.showing
    assert not root._job_start.held


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (JOB_START_REASON_ABORT, "cancelled from a controller"),
        (JOB_START_REASON_STARTER_LEFT, "started it disconnected"),
        (JOB_START_REASON_HALT, "machine halted"),
    ],
)
def test_a_cancelled_start_closes_the_countdown_and_says_why(tmp_path, app, reason, expected):
    root = _host(tmp_path)
    Makera.on_job_start_event(root, _event(not_ready=(THIRD,)))

    Makera.on_job_start_event(root, _event(phase=JOB_START_CANCELLED, reason=reason, seconds_left=0, not_ready=()))

    assert not root._job_start_popup.showing
    message = root.show_message_popup.call_args.args[0]
    assert "air-test-long.nc" in message
    assert expected in message


def test_a_start_at_the_time_limit_names_who_was_not_waited_for(tmp_path, app):
    root = _host(tmp_path, own_id=PC, has_control=True)
    Makera.on_job_start_event(root, _event(not_ready=(DEMO,), seconds_left=1))

    Makera.on_job_start_event(
        root, _event(phase=JOB_START_STARTING, reason=JOB_START_REASON_TIME_LIMIT, seconds_left=0, not_ready=(DEMO,))
    )

    assert not root._job_start_popup.showing
    root.show_message_popup.assert_not_called()
    assert any("time limit" in text and "Demo" in text for text in _logged(root))


def test_a_fetch_still_running_when_the_job_starts_draws_but_sends_no_ready(tmp_path, app, monkeypatch):
    """The start came at the limit (or by Start now) while this controller
    was still fetching: the file is still drawn once fetched, and no ready
    goes out for a start that is over."""
    _HeldThread.started = []
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _HeldThread)
    root = _host(tmp_path)
    Makera.on_job_start_event(root, _event())
    Makera.on_job_start_event(
        root, _event(phase=JOB_START_STARTING, reason=JOB_START_REASON_TIME_LIMIT, seconds_left=0)
    )

    _HeldThread.started[0].run()
    _HeldThread.started[1].run()

    root.load_gcode_file.assert_called_once()
    root.controller.send_job_start_ready.assert_not_called()
    assert root._job_without_toolpath is None


def test_a_fetch_failing_after_the_job_started_shows_it_without_toolpath(tmp_path, app, monkeypatch):
    _HeldThread.started = []
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _HeldThread)
    root = _host(tmp_path)
    root.doDownload = _download_results(None)
    Makera.on_job_start_event(root, _event())
    Makera.on_job_start_event(
        root, _event(phase=JOB_START_STARTING, reason=JOB_START_REASON_START_NOW, seconds_left=12)
    )
    assert root._job_without_toolpath is None  # decided once the fetch ends

    _HeldThread.started[0].run()

    root.controller.send_job_start_ready.assert_not_called()
    assert root._job_without_toolpath == PATH
    assert root._passive_fetch.pending_path == PATH


def test_disconnecting_forgets_the_hold(tmp_path, app):
    root = _host(tmp_path)
    Makera.on_job_start_event(root, _event(not_ready=(THIRD,)))

    root._job_start.clear()
    Makera._close_job_start_countdown(root)

    assert not root._job_start_popup.showing


# -- Controller ---------------------------------------------------------------


def _controller(monkeypatch):
    c = Controller(CNC(), callback=None, identity=ControllerIdentity(id=DEMO, name="Demo"))
    sent = []
    c.stream = SimpleNamespace(send=lambda frame: sent.append(frame))
    c._hello = SimpleNamespace(identified=True, machine_holds_starts=True)
    monkeypatch.setattr("carveracontroller.Controller.App", None)
    return c, sent


def _event_message(payload):
    return ParsedMessage(MessageKind.EVENT, payload=payload)


def test_controller_hands_a_job_start_event_to_the_ui(monkeypatch):
    from tests.unit.test_job_start_wire import job_start_payload

    c, _ = _controller(monkeypatch)
    handed = []
    c._notify_job_start = handed.append

    c._handle_protocol_message(_event_message(job_start_payload(start_id=9)))

    assert c.last_job_start_event is not None
    assert c.last_job_start_event.start_id == 9
    assert [e.start_id for e in handed] == [9]


def test_controller_passes_play_started_size_and_checksum_on(monkeypatch):
    c, _ = _controller(monkeypatch)
    published = []
    c._notify_file_published = lambda *a: published.append(a)
    payload = bytes([EVENT_KIND_PLAY_STARTED, len(PATH)]) + PATH.encode() + len(CONTENT).to_bytes(4, "big")
    payload += bytes([1]) + MD5

    c._handle_protocol_message(_event_message(payload))

    assert published == [(PATH, MD5, len(CONTENT), True)]


def test_controller_with_old_play_started_passes_no_size(monkeypatch):
    c, _ = _controller(monkeypatch)
    published = []
    c._notify_file_published = lambda *a: published.append(a)

    c._handle_protocol_message(_event_message(bytes([EVENT_KIND_PLAY_STARTED, len(PATH)]) + PATH.encode()))

    assert published == [(PATH, b"", None, True)]


def test_send_job_start_ready_sends_the_ready_frame(monkeypatch):
    c, sent = _controller(monkeypatch)
    assert c.send_job_start_ready(0x0102) is True
    assert sent == [build_frame(0x6C, bytes([0x01, 0x02]))]


def test_send_job_start_ready_needs_an_identified_link(monkeypatch):
    c, sent = _controller(monkeypatch)
    c._hello = SimpleNamespace(identified=False, machine_holds_starts=False)
    assert c.send_job_start_ready(1) is False
    c._hello = None
    assert c.send_job_start_ready(1) is False
    assert sent == []


def test_start_now_sends_the_start_now_command(monkeypatch):
    c, _ = _controller(monkeypatch)
    c.executeCommand = MagicMock()
    c.startNowCommand()
    c.executeCommand.assert_called_once_with("start-now\n")


def test_refusal_during_a_hold_is_shown_but_is_no_alarm(monkeypatch):
    c, _ = _controller(monkeypatch)
    CNC.vars["alarm_message"] = ""
    c.parseLine(JOB_START_REFUSAL)
    logged = []
    while not c.log.empty():
        logged.append(c.log.get_nowait())
    assert (Controller.MSG_ERROR, JOB_START_REFUSAL) in logged
    assert CNC.vars["alarm_message"] == ""


def test_old_firmware_never_holds_a_start(monkeypatch):
    c, _ = _controller(monkeypatch)
    c._hello = HelloNegotiator(identity=ControllerIdentity(id=DEMO, name="Demo"), link=0)
    c._hello.on_valid_frame(0.0)
    c._on_hello_ack(bytes([HELLO_PROTOCOL_VERSION, HELLO_ACCEPTED, 0]))  # three bytes: no features
    assert c._status_subscribed()
    assert c.machine_holds_starts is False


# -- resume at a line ---------------------------------------------------------


@pytest.fixture
def resume_controller(monkeypatch):
    c, _ = _controller(monkeypatch)
    c._get_line_position_from_gcode_viewer = lambda _line: (None, None, None, None)
    c.executeCommand = MagicMock()
    scheduled = []
    monkeypatch.setattr(
        "carveracontroller.Controller.Clock", SimpleNamespace(schedule_once=lambda cb, *a: scheduled.append(cb))
    )
    lines = ["G21\n", "G90\n", "G0 X1\n", "G1 X2 F300\n", "G1 X3\n"]
    return c, scheduled, lines


def _sent(c):
    return [call.args[0] for call in c.executeCommand.call_args_list]


def _tick(scheduled):
    pending = list(scheduled)
    scheduled.clear()
    for cb in pending:
        cb(0.1)


def test_resume_at_line_waits_through_the_hold(resume_controller):
    """buffer M600 and play go out; the machine holds the start (state
    stays Idle, waiting events arrive) and the rest waits. Once the job
    starts and pauses on M600, the rest is sent."""
    c, scheduled, lines = resume_controller
    CNC.vars["state"] = "Idle"
    c.playStartLineCommand("job.nc", 4, lines=lines)
    sent = _sent(c)
    assert sent[0] == "buffer M600"
    assert sent[-1].startswith("play")

    for seconds in (30, 29, 28):
        c._on_job_start(_event(seconds_left=seconds, starter_id=DEMO, not_ready=(PC,)))
        _tick(scheduled)
    assert len(_sent(c)) == len(sent)

    c._on_job_start(_event(phase=JOB_START_STARTING, reason=JOB_START_REASON_ALL_READY, not_ready=()))
    CNC.vars["state"] = "Pause"
    _tick(scheduled)

    after = _sent(c)[len(sent) :]
    assert after[0].startswith("goto")
    assert after[-1] == "resume"


def test_a_cancelled_start_ends_the_resume_wait(resume_controller):
    """The machine drops the buffered lines when a held start is cancelled
    and never pauses. The rest of the resume must never be sent, even if a
    later job pauses."""
    c, scheduled, lines = resume_controller
    CNC.vars["state"] = "Idle"
    c.playStartLineCommand("job.nc", 4, lines=lines)
    count = len(_sent(c))

    c._on_job_start(_event(phase=JOB_START_CANCELLED, reason=JOB_START_REASON_ABORT, seconds_left=0))
    CNC.vars["state"] = "Pause"
    _tick(scheduled)
    _tick(scheduled)

    assert len(_sent(c)) == count
    assert scheduled == []


# -- operator actions held during a job-start fetch ---------------------------


@pytest.mark.parametrize("download_works", [True, False])
def test_an_operator_action_held_during_a_job_start_fetch_runs_after_it(tmp_path, app, monkeypatch, download_works):
    """The operator asks for a listing while the fetch for a held start is
    downloading: it waits, and runs once the fetch has ended, whether the
    download worked or not."""
    _HeldThread.started = []
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _HeldThread)
    root = _host(tmp_path)
    root.progressStart = MagicMock()
    root.progressFinish = MagicMock()
    if not download_works:
        root.doDownload = _download_results(None)
    action = MagicMock()

    Makera.on_job_start_event(root, _event())
    assert Makera._hold_for_passive_fetch(root, action) is True
    action.assert_not_called()

    _HeldThread.started[0].run()  # the download
    if download_works:
        _HeldThread.started[1].run()  # the drawing

    action.assert_called_once()
    assert root._auto_fetch_in_progress is False
    assert root._held_for_passive_fetch == ()
    if download_works:
        root.controller.send_job_start_ready.assert_called_once_with(7)
    else:
        root.controller.send_job_start_ready.assert_not_called()


def test_a_job_start_fetch_waits_for_a_held_operator_action(tmp_path, app, monkeypatch):
    _HeldThread.started = []
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _HeldThread)
    root = _host(tmp_path)
    root._held_for_passive_fetch = (MagicMock(),)

    Makera.on_job_start_event(root, _event())
    assert _HeldThread.started == []

    root._held_for_passive_fetch = ()
    Makera.on_job_start_event(root, _event(seconds_left=29))
    assert len(_HeldThread.started) == 1
