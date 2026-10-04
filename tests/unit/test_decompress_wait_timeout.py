"""Tests for the "Decompressing" wait timeout.

main.py's monitorSerial advances the post-upload "Decompressing" progress
bar from the firmware's own replies (Player::decompress, Player.cpp): a
"#Info: decompart = N" line every 11 blocks, plus one more, unconditional,
once the whole file is unpacked. Before this fix, if that comparison ever
stopped changing -- the reply lost, or the decompress itself hanging or
erroring out on the machine -- the only fallback silently treated the
stall as a finished transfer, after a fixed 8 seconds, by calling
updateCompressProgress(self.fileCompressionBlocks) regardless of what
actually happened on the machine; and for a source file that compresses to
zero blocks (an empty file), that same call divides by zero
(value * 100.0 / self.fileCompressionBlocks) and kills the monitorSerial
thread outright, so the wait truly never ends.

These tests drive the extracted logic (_update_decompress_progress,
_decompress_timed_out, and the division guard in updateCompressProgress)
directly, the same style test_md5_verify_upload.py and test_file_browser.py
use: an unbound Makera method with a SimpleNamespace standing in for self.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from carveracontroller.Controller import Controller
from carveracontroller.main import DECOMPRESS_TIMEOUT_FLOOR_SEC, DECOMPRESS_TIMEOUT_PER_BLOCK_SEC, Makera
from carveracontroller.translation import tr


def _host(*, blocks, decompercent=0, decompercentlast=0, decomptime=0.0):
    host = SimpleNamespace(
        fileCompressionBlocks=blocks,
        decompercent=decompercent,
        decompercentlast=decompercentlast,
        decomptime=decomptime,
        decompstatus=True,
        pending_decompress_callback=None,
        controller=SimpleNamespace(log=SimpleNamespace(put=MagicMock())),
        progressUpdate=MagicMock(),
        progressFinish=MagicMock(),
        show_message_popup=MagicMock(),
        file_popup=SimpleNamespace(refresh_machine=MagicMock()),
    )
    # The real methods (not stubs): _update_decompress_progress calls both
    # of these on self (the zero-block/"changed" branches call
    # updateCompressProgress; the timeout branch calls _decompress_timed_out).
    host.updateCompressProgress = lambda value: Makera.updateCompressProgress(host, value)
    host._decompress_timed_out = lambda: Makera._decompress_timed_out(host)
    return host


def _run_scheduled_immediately(monkeypatch):
    """Clock.schedule_once runs its callback right away with dt=0, so the
    side effects it schedules (progressUpdate, progressFinish,
    show_message_popup, file_popup.refresh_machine) are visible without a
    real Kivy event loop."""
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, t=0, *a, **kw: cb(0))


# -----------------------------------------------------------------------
# A real progress reply always resets the watchdog
# -----------------------------------------------------------------------


def test_progress_update_advances_bar_and_resets_watchdog(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    host = _host(blocks=40, decompercent=11, decompercentlast=0, decomptime=1000.0)

    Makera._update_decompress_progress(host, now=1005.0)

    assert host.decompercentlast == 11
    assert host.decomptime == 1005.0
    assert host.decompstatus is True  # not finished yet: 11 != 40 blocks
    host.controller.log.put.assert_not_called()


# -----------------------------------------------------------------------
# A stall shorter than the watchdog's timeout changes nothing
# -----------------------------------------------------------------------


def test_stall_inside_the_timeout_keeps_waiting(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    host = _host(blocks=2, decomptime=1000.0)

    Makera._update_decompress_progress(host, now=1000.0 + DECOMPRESS_TIMEOUT_FLOOR_SEC - 1)

    assert host.decompstatus is True
    host.controller.log.put.assert_not_called()
    host.show_message_popup.assert_not_called()
    host.progressFinish.assert_not_called()


# -----------------------------------------------------------------------
# The watchdog: a stall past the timeout ends the upload with a clear
# message, instead of hanging forever (the bug) or the old fallback of
# silently treating a stall as a finished transfer.
# -----------------------------------------------------------------------


def test_stall_past_the_timeout_ends_the_upload_with_a_message(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    host = _host(blocks=2, decomptime=1000.0)
    host.pending_decompress_callback = MagicMock()

    Makera._update_decompress_progress(host, now=1000.0 + DECOMPRESS_TIMEOUT_FLOOR_SEC + 1)

    assert host.decompstatus is False
    assert host.pending_decompress_callback is None
    host.progressFinish.assert_called_once()
    expected_message = tr._("Decompressing on the machine timed out; the file may not be usable.")
    host.controller.log.put.assert_called_once_with((Controller.MSG_ERROR, expected_message))
    host.show_message_popup.assert_called_once_with(expected_message, False, 0)


def test_decompress_timed_out_is_safe_to_call_directly(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    host = _host(blocks=2)
    host.decompstatus = False  # not normally reachable, but must stay harmless

    Makera._decompress_timed_out(host)

    assert host.decompstatus is False
    host.progressFinish.assert_called_once()


# -----------------------------------------------------------------------
# The timeout scales with the file's block count, with a floor for small
# files -- Player::decompress only reports progress every 11 blocks, so a
# small file may only ever see the one, final, unconditional reply.
# -----------------------------------------------------------------------


def test_timeout_uses_the_floor_for_a_small_file(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    blocks = 2
    assert DECOMPRESS_TIMEOUT_PER_BLOCK_SEC * blocks < DECOMPRESS_TIMEOUT_FLOOR_SEC
    host = _host(blocks=blocks, decomptime=1000.0)

    Makera._update_decompress_progress(host, now=1000.0 + DECOMPRESS_TIMEOUT_FLOOR_SEC - 0.5)
    assert host.decompstatus is True

    Makera._update_decompress_progress(host, now=1000.0 + DECOMPRESS_TIMEOUT_FLOOR_SEC + 0.5)
    assert host.decompstatus is False


def test_timeout_scales_up_for_a_large_file(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    blocks = 10_000  # far more than the floor alone would cover
    per_block_timeout = DECOMPRESS_TIMEOUT_PER_BLOCK_SEC * blocks
    assert per_block_timeout > DECOMPRESS_TIMEOUT_FLOOR_SEC
    host = _host(blocks=blocks, decomptime=1000.0)

    Makera._update_decompress_progress(host, now=1000.0 + per_block_timeout - 1)
    assert host.decompstatus is True

    Makera._update_decompress_progress(host, now=1000.0 + per_block_timeout + 1)
    assert host.decompstatus is False


# -----------------------------------------------------------------------
# Edge case: a source file that compresses to zero blocks (an empty file)
# must finish at once rather than dividing by zero in
# updateCompressProgress's value/fileCompressionBlocks ratio.
# -----------------------------------------------------------------------


def test_zero_blocks_finishes_immediately_without_dividing_by_zero(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    host = _host(blocks=0, decomptime=1000.0)

    Makera._update_decompress_progress(host, now=1000.0)

    assert host.decompstatus is False
    host.progressFinish.assert_called_once()
    host.controller.log.put.assert_not_called()  # a real finish, not a timeout error


def test_update_compress_progress_zero_blocks_does_not_raise(monkeypatch):
    _run_scheduled_immediately(monkeypatch)
    host = _host(blocks=0)

    Makera.updateCompressProgress(host, 0)  # must not raise ZeroDivisionError

    assert host.decompstatus is False
    host.progressUpdate.assert_called_once_with(100.0, "", True, 0)
