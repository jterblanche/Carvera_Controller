"""Tests for a decompress wait surviving a lost link.

doUpload sets self.decompstatus after a compressed (".lz") upload's FILE
end flag arrives, to wait for the machine's own "decompart"/completion
replies (see _update_decompress_progress). Before this fix, nothing ended
that wait when the link went away first: _link_busy_for_passive_fetch
counts decompstatus as link-busy, so a lost connection left every passive
fetch -- including a held start's preparation -- blocked until the
watchdog's timeout (_decompress_timed_out), up to about 85 minutes for a
20 MiB file at 4 KiB blocks.

Two disconnect paths both need to end the wait, so both are covered here:

- updateStatus's NOT_CONNECTED handling (_clear_decompress_wait_on_disconnect),
  reached on the state's edge from connected to NOT_CONNECTED. Most of this
  file drives that method directly, the same style test_decompress_wait_timeout.py
  uses: an unbound Makera method with a SimpleNamespace standing in for
  self. test_updatestatus_through_a_real_peer_close below instead goes
  through the real Controller._close_inline() and Makera.updateStatus(),
  reusing test_screen_after_inline_close.py's helpers, so the hook-up
  itself -- not just the extracted method -- is covered.
- _update_decompress_progress's own connection check (added alongside
  _clear_decompress_wait_on_disconnect): doUpload can still be between the
  transfer finishing and setting self.decompstatus -- copying the uploaded
  file and its original locally -- at the moment the link drops. That is
  after the state's edge updateStatus reacts to has already been handled,
  so nothing would otherwise clear decompstatus a second time until the
  watchdog's own, much longer, timeout.

doUpload's own success path (_upload_file) also sets decomptime and
pending_decompress_callback before decompstatus, not after: decompstatus is
what a concurrent monitorSerial tick gates on, so everything that tick could
read once it sees decompstatus True must already be in its final state by
then, not read mid-assignment. test_decompstatus_is_set_last_in_the_lz_success_path
proves that ordering directly; test_file_end_flag_then_lost_link_leaves_no_stale_callback
drives the whole thing end to end, through the real upload path, from a
successful transfer to a lost link before the machine's decompart
confirmation ever arrives.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from carveracontroller.CNC import CNC
from carveracontroller.Controller import Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.main import Makera
from carveracontroller.translation import tr
from tests.unit.test_screen_after_inline_close import _connected_app, _connected_screen
from tests.unit.test_upload_compress_failure import _host as _real_upload_host
from tests.unit.test_upload_compress_failure import _ImmediateThread

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")


def _host(*, decompstatus, pending_decompress_callback=None, stream=None):
    host = SimpleNamespace(
        decompstatus=decompstatus,
        pending_decompress_callback=pending_decompress_callback,
        controller=SimpleNamespace(
            log=SimpleNamespace(put=MagicMock()),
            sendNUM=0,
            loadNUM=0,
            stream=stream,
        ),
        progressFinish=MagicMock(),
        show_message_popup=MagicMock(),
        _link_workers=0,
        uploading=False,
        downloading=False,
        backing_up_config=False,
        # Only exercised by test_update_decompress_progress_clears_a_late_wait
        # below; harmless for every other test here.
        fileCompressionBlocks=10_000,
        decompercent=0,
        decompercentlast=0,
        decomptime=0.0,
    )
    host._end_decompress_wait = lambda message: Makera._end_decompress_wait(host, message)
    host._clear_decompress_wait_on_disconnect = lambda: Makera._clear_decompress_wait_on_disconnect(host)
    host._update_decompress_progress = lambda now: Makera._update_decompress_progress(host, now)
    return host


def _run_scheduled_immediately(monkeypatch):
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, t=0, *a, **kw: cb(0))


# -----------------------------------------------------------------------
# Losing the link while a FILE end flag's decompress wait is still
# running ends the wait immediately, with a clear message, instead of
# leaving it for the long watchdog.
# -----------------------------------------------------------------------


def test_lost_link_ends_a_running_decompress_wait(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    callback = MagicMock()
    host = _host(decompstatus=True, pending_decompress_callback=callback)

    host._clear_decompress_wait_on_disconnect()

    assert host.decompstatus is False
    assert host.pending_decompress_callback is None
    callback.assert_not_called()
    host.progressFinish.assert_called_once()
    expected_message = tr._("Connection lost while decompressing on the machine; the file may not be usable.")
    host.controller.log.put.assert_called_once_with((Controller.MSG_ERROR, expected_message))
    host.show_message_popup.assert_called_once_with(expected_message, False, 0)


# -----------------------------------------------------------------------
# An ordinary disconnect with no decompress wait running must stay quiet:
# no spurious error logged or shown for every reconnect.
# -----------------------------------------------------------------------


def test_disconnect_without_a_decompress_wait_is_a_no_op(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    host = _host(decompstatus=False)

    host._clear_decompress_wait_on_disconnect()

    assert host.decompstatus is False
    host.controller.log.put.assert_not_called()
    host.show_message_popup.assert_not_called()
    host.progressFinish.assert_not_called()


# -----------------------------------------------------------------------
# After a reconnect, with the wait cleared, the link is no longer busy on
# decompstatus's account, so passive fetches and held-start preparation
# can run again.
# -----------------------------------------------------------------------


def test_link_no_longer_busy_for_passive_fetch_after_the_wait_is_cleared(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    host = _host(decompstatus=True)
    assert Makera._link_busy_for_passive_fetch(host) is True  # sanity: decompstatus alone blocks it

    host._clear_decompress_wait_on_disconnect()

    assert Makera._link_busy_for_passive_fetch(host) is False


# -----------------------------------------------------------------------
# The race this fix also closes: doUpload sets self.decompstatus=True only
# after the transfer finishes and the uploaded file and its original are
# copied locally -- real work that takes real time. If the link drops
# during that window, updateStatus's NOT_CONNECTED edge has already been
# and gone by the time decompstatus becomes True, so
# _clear_decompress_wait_on_disconnect never runs for it. Proven by a
# large fileCompressionBlocks (so the pre-existing per-block watchdog,
# which only reacts after max(15, blocks) seconds, cannot be what ends
# this): the connection check at the top of _update_decompress_progress is
# the only thing that can end this wait on the very next monitorSerial
# tick.
# -----------------------------------------------------------------------


def test_update_decompress_progress_clears_a_wait_that_started_after_disconnect(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    callback = MagicMock()
    # stream=None: the link is already gone by the time this tick runs,
    # the same as if decompstatus had only just been set True on a
    # connection that updateStatus already handled losing.
    host = _host(decompstatus=True, pending_decompress_callback=callback, stream=None)

    host._update_decompress_progress(now=0.001)  # far short of the watchdog's own timeout

    assert host.decompstatus is False
    assert host.pending_decompress_callback is None
    callback.assert_not_called()
    expected_message = tr._("Connection lost while decompressing on the machine; the file may not be usable.")
    host.controller.log.put.assert_called_once_with((Controller.MSG_ERROR, expected_message))


def test_update_decompress_progress_leaves_a_running_wait_alone_while_connected(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    host = _host(decompstatus=True, stream=MagicMock())  # connected: a real stream object

    host._update_decompress_progress(now=0.001)  # far short of the watchdog's own timeout

    assert host.decompstatus is True  # untouched: nothing wrong with this wait yet
    host.controller.log.put.assert_not_called()


# -----------------------------------------------------------------------
# The real hook-up, not just the extracted method: Controller._close_inline
# (a USB PeerClosedError, a WiFi peer close, ...) followed by the popup
# that always follows it must leave decompstatus cleared too, the same way
# it already clears the md5-verify wait next to it in updateStatus.
# -----------------------------------------------------------------------


def test_updatestatus_through_a_real_peer_close_clears_the_wait(monkeypatch, connected_idle_state):
    controller = Controller(CNC(), callback=None, identity=IDENTITY)
    controller.start_reconnection = MagicMock()
    app = _connected_app()
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: app)
    root = _connected_screen(controller)
    callback = MagicMock()
    root.decompstatus = True
    root.pending_decompress_callback = callback
    root._link_workers = 0
    root.uploading = False
    root.downloading = False
    root.backing_up_config = False

    controller._close_inline()
    root.show_peer_closed_popup()

    assert root.decompstatus is False
    assert root.pending_decompress_callback is None
    callback.assert_not_called()
    assert Makera._link_busy_for_passive_fetch(root) is False


def test_updatestatus_is_what_the_fix_relies_on(monkeypatch, connected_idle_state):
    """A regression guard for the hook-up itself, not just the method it
    calls: with Makera._clear_decompress_wait_on_disconnect monkeypatched
    away, the same real disconnect path above must leave decompstatus
    exactly as it found it, proving updateStatus's NOT_CONNECTED branch is
    the (only) thing calling it on this path."""
    monkeypatch.setattr(Makera, "_clear_decompress_wait_on_disconnect", lambda self: None)
    controller = Controller(CNC(), callback=None, identity=IDENTITY)
    controller.start_reconnection = MagicMock()
    app = _connected_app()
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: app)
    root = _connected_screen(controller)
    root.decompstatus = True
    root._link_workers = 0
    root.uploading = False
    root.downloading = False
    root.backing_up_config = False

    controller._close_inline()
    root.show_peer_closed_popup()

    assert root.decompstatus is True  # unchanged: proves the call site, not just the method, matters


# -----------------------------------------------------------------------
# _upload_file's own ordering guarantee: decompstatus is set last, after
# decomptime and pending_decompress_callback, so a concurrent monitorSerial
# tick can never see decompstatus True before the data it reads under that
# flag is already final. A data descriptor installed in decompstatus's
# place records what the other two attributes held at the exact moment
# decompstatus is set True -- the same moment a real tick could first
# observe it -- without needing a real second thread.
# -----------------------------------------------------------------------


class _RecordsStateWhenDecompstatusIsSetTrue:
    """Installed (via monkeypatch) in place of Makera's decompstatus class
    attribute: a data descriptor, so it intercepts every instance's
    ``self.decompstatus = ...``. Records decomptime and
    pending_decompress_callback at the exact moment decompstatus is set
    True -- the same moment a real concurrent monitorSerial tick could
    first observe it -- storing them under the instance's own
    __dict__ rather than relying on __set_name__ (never called by a plain
    setattr after the class already exists)."""

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return obj.__dict__.get("_decompstatus", False)

    def __set__(self, obj, value):
        obj.__dict__["_decompstatus"] = value
        if value:
            obj.decomptime_when_flag_was_set = obj.decomptime
            obj.pending_decompress_callback_when_flag_was_set = obj.pending_decompress_callback


def test_decompstatus_is_set_last_in_the_lz_success_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Makera, "decompstatus", _RecordsStateWhenDecompstatusIsSetTrue())
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: None)
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    folder = tmp_path / "jobs"
    folder.mkdir()
    job = folder / "job.nc"
    job.write_bytes(b"G21\nG0 X0\nG0 X1\n" * 50)
    cache = tmp_path / "cache"
    cache.mkdir()
    root = _real_upload_host(tmp_path, cache)
    callback = MagicMock()
    before = time.time()

    Makera.uploadLocalFile(root, str(job), callback=callback)

    assert root.uploading_file.endswith(".lz")  # sanity: this is the compressed-upload path
    # decomptime was not still the class default (0) or some earlier
    # upload's stale value at the moment decompstatus became True -- it was
    # already this upload's own, fresh timestamp.
    assert root.decomptime_when_flag_was_set >= before
    # pending_decompress_callback was not still unset (or some earlier
    # upload's stale value) at that same moment either.
    assert root.pending_decompress_callback_when_flag_was_set is not None


# -----------------------------------------------------------------------
# The ticket's own scenario, end to end through the real upload path: a
# compressed upload's FILE end flag arrives (the transfer succeeds, so
# decompstatus and the pending callback for it are set together in
# _upload_file), and then the link is lost before the machine's own
# decompart confirmation ever comes back. The callback must never fire for
# this upload, and nothing stale is left behind for an unrelated later
# decompress to fire by mistake.
# -----------------------------------------------------------------------


def test_file_end_flag_then_lost_link_leaves_no_stale_callback(tmp_path, monkeypatch):
    scheduled = []
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: scheduled.append(cb))
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    folder = tmp_path / "jobs"
    folder.mkdir()
    job = folder / "job.nc"
    job.write_bytes(b"G21\nG0 X0\nG0 X1\n" * 50)
    cache = tmp_path / "cache"
    cache.mkdir()
    root = _real_upload_host(tmp_path, cache)
    callback = MagicMock()

    Makera.uploadLocalFile(root, str(job), callback=callback)  # the FILE end flag: the transfer succeeded

    assert root.uploading_file.endswith(".lz")  # sanity: this is the compressed-upload path
    assert root.decompstatus is True
    assert root.pending_decompress_callback is not None
    callback.assert_not_called()  # not yet -- still waiting for the machine's decompart confirmation

    # The link is lost before that confirmation ever arrives.
    root.controller.stream = None
    Makera._clear_decompress_wait_on_disconnect(root)

    assert root.decompstatus is False
    assert root.pending_decompress_callback is None
    callback.assert_not_called()  # never fired, and nothing left to fire it later with this upload's name
