"""Integration test: the "Decompressing" popup after a compressed upload.

After a .lz upload, doUpload (on its own thread) arms the decompress wait
and schedules the "Decompressing" popup 0.2 s later on the Kivy clock,
leaving the "Uploading" popup time to close first. The wait ends when
monitorSerial sees the firmware's final "#Info: decompart = N" reply, which
schedules progressFinish at once. For a small file the machine finishes
unpacking before that 0.2 s has passed (the final reply can arrive in the
same read as the upload's own success reply), so the finish runs first and
the popup must not open afterwards with nothing left to close it.

These tests run the real upload entry point (uploadLocalFile, which
compresses the file and starts doUpload on a thread), the real Kivy clock
and the real progress popup. Only the transfer and the housekeeping after
it (directory refresh, thumbnail, recent folders) are stubbed, and the
firmware reply is applied by the test (the same state change monitorSerial
makes, through the same _update_decompress_progress call) at a chosen point
in the clock's timeline, so the ordering is deterministic.
"""

import time
from unittest.mock import MagicMock

import pytest
from kivy.clock import Clock

from carveracontroller.main import Makera
from tests.integration.conftest import pump_frames

FRAME_SEC = 0.01


def _frame():
    # Clock ticks only, without drawing a frame: drawing the whole window
    # under software rendering can take longer than the popup's 0.2 s delay,
    # and the clock has to keep ticking well inside that delay for the
    # order of events below to be the one a running app sees.
    Clock.tick()
    time.sleep(FRAME_SEC)


def _pump_for(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _frame()


def _pump_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the upload to reach the decompress wait"
        _frame()


def _deliver_final_decompress_reply(root):
    """What monitorSerial does when "#Info: decompart = N" arrives with N
    equal to the file's block count: its regex sets decompercent, then the
    decompstatus branch calls _update_decompress_progress."""
    root.decompercent = root.fileCompressionBlocks
    Makera._update_decompress_progress(root, time.time())


@pytest.fixture
def compressed_upload(kivy_app, tmp_path, monkeypatch):
    """Prepare a successful .lz upload with the transfer faked out, and
    return a function that starts it and returns once doUpload has armed
    the decompress wait."""
    root = kivy_app.root
    popup = root.progress_popup

    source = tmp_path / "small.nc"
    source.write_bytes(b"G0 X0 Y0\nG1 X10 Y10 F500\n" * 200)

    stream = MagicMock()
    stream.upload.return_value = True
    stream.modem = None
    monkeypatch.setattr(root.controller, "stream", stream)
    monkeypatch.setattr(root.controller, "uploadCommand", lambda name: True)
    monkeypatch.setattr(root, "filetype", "lz")
    monkeypatch.setattr(root.file_popup, "firmware_mode", False)
    monkeypatch.setattr(root.file_popup, "refresh_machine", lambda *args: None)
    monkeypatch.setattr(root, "update_recent_local_dir_list", lambda *args: None)
    monkeypatch.setattr(root, "queue_machine_thumbnail", lambda *args: None)
    # The test applies the firmware's reply itself; keep the running
    # monitorSerial thread from acting on the decompress wait on its own
    # schedule, so the order of events is the one each test sets up.
    monkeypatch.setattr(root, "_update_decompress_progress", lambda now: None)

    def start():
        pump_frames(2)  # a fresh clock tick, so scheduled delays count from now
        root.uploadLocalFile(str(source))
        _pump_until(lambda: root.decompstatus and not root.uploading, timeout=10)
        assert root.fileCompressionBlocks > 0

    yield start

    root.decompstatus = False
    root.pending_decompress_callback = None
    if popup._is_open:
        popup.dismiss()
    pump_frames(20, sleep=0.02)


def test_decompress_finishing_before_the_popup_opens_leaves_no_popup(kivy_app, compressed_upload):
    root = kivy_app.root
    popup = root.progress_popup

    compressed_upload()
    # The machine has already finished: the final reply is handled on the
    # first frame after the wait is armed, well inside the popup's delay.
    _deliver_final_decompress_reply(root)
    assert root.decompstatus is False

    _pump_for(1.0)  # past the popup's delay and every dismiss animation

    assert popup._is_open is False, f"progress popup left open: {popup.progress_text!r}"
    assert popup.parent is None


def test_decompress_finishing_after_the_popup_opens_closes_it(kivy_app, compressed_upload):
    """Contrast case: a decompress that outlasts the delay still shows the
    popup while it runs, and the final reply closes it."""
    root = kivy_app.root
    popup = root.progress_popup

    compressed_upload()
    _pump_for(0.6)

    assert popup._is_open is True
    assert popup.progress_text.startswith("Decompressing")

    _deliver_final_decompress_reply(root)
    _pump_for(1.0)

    assert root.decompstatus is False
    assert popup._is_open is False
    assert popup.parent is None
