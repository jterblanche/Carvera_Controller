"""Starting an upload never leaves the link marked busy, even when the
file cannot be compressed or the upload thread cannot start.

uploadLocalFile marks the link as busy with an upload (``sendNUM``) on the
main thread, compresses the file when the machine accepts compressed
uploads, and then starts doUpload on a worker thread, which clears the mark
however it ends. Anything that raises between the mark and the thread start
would leave the mark set, so the controller would stop asking for status.

Compression falls back to the uncompressed file when it fails. When the
file cannot be compressed beside the original, it is copied into the cache
directory and compressed there; if that copy fails too, the fallback must
still happen, with a note in the console.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller.Controller import SEND_FILE
from carveracontroller.machine.local_copies import LocalCopyStore
from carveracontroller.machine.passive_fetch import PassiveFetchTracker
from carveracontroller.main import Makera


class _ImmediateThread:
    def __init__(self, target=None, args=(), **_ignored):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


class _ThreadThatCannotStart:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


@pytest.fixture
def scheduled(monkeypatch):
    calls = []
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: calls.append(cb))
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)
    return calls


def _host(tmp_path, cache_dir):
    root = Makera.__new__(Makera)
    root.temp_dir = str(cache_dir)
    root.filetype = "lz"
    root._passive_fetch = PassiveFetchTracker()
    # load_gcode_file and a finished upload remember the file as a local copy.
    root._local_copies = LocalCopyStore()
    root._uploading_firmware = False
    root.uploading = False
    root.decompstatus = False
    root.file_popup = SimpleNamespace(machine_dir="/sd/gcodes", firmware_mode=False, refresh_machine=MagicMock())
    root.controller = SimpleNamespace(
        sendNUM=0,
        loadNUM=0,
        pauseStream=MagicMock(),
        resumeStream=MagicMock(),
        uploadCommand=MagicMock(return_value=True),
        stream=SimpleNamespace(upload=MagicMock(return_value=True)),
        log=MagicMock(),
    )
    root.update_recent_local_dir_list = MagicMock()
    root.queue_machine_thumbnail = MagicMock()
    root._cleanup_firmware_temp = MagicMock()
    root.show_message_popup = MagicMock()
    return root


def _read_only_job(tmp_path):
    """A job in a folder where no ``.lz`` can be written beside it."""
    folder = tmp_path / "jobs"
    folder.mkdir()
    job = folder / "job.nc"
    job.write_bytes(b"G0 X0\nG0 X1\n")
    folder.chmod(0o555)
    return job


@pytest.fixture
def read_only_job(tmp_path):
    job = _read_only_job(tmp_path)
    if os.access(job.parent, os.W_OK):
        pytest.skip("running with permissions that ignore a read-only folder")
    yield job
    job.parent.chmod(0o755)


def _logged(root):
    return [call.args[0][1] for call in root.controller.log.put.call_args_list]


def test_compress_falls_back_when_the_cache_copy_fails_too(tmp_path, read_only_job, scheduled):
    """No ``.lz`` beside the job, and the cache directory cannot be written
    either: compress_file gives up without raising."""
    root = _host(tmp_path, tmp_path / "missing" / "cache")

    assert Makera.compress_file(root, str(read_only_job)) is None


def test_upload_goes_ahead_uncompressed_when_compressing_fails(tmp_path, read_only_job, scheduled, monkeypatch):
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    root = _host(tmp_path, tmp_path / "missing" / "cache")

    Makera.uploadLocalFile(root, str(read_only_job))

    root.controller.stream.upload.assert_called_once()
    assert root.controller.stream.upload.call_args.args[0] == str(read_only_job)
    assert root.controller.sendNUM == 0
    assert root.uploading is False
    assert any("job.nc" in line and "uncompressed" in line for line in _logged(root))


def test_upload_thread_that_cannot_start_clears_send_state_and_says_so(tmp_path, scheduled, monkeypatch):
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ThreadThatCannotStart)
    root = _host(tmp_path, tmp_path / "cache")
    root.filetype = ""
    job = tmp_path / "job.nc"
    job.write_bytes(b"G0 X0\n")

    Makera.uploadLocalFile(root, str(job))

    assert root.controller.sendNUM == 0
    assert root.uploading is False
    assert root._uploading_firmware is False
    root._cleanup_firmware_temp.assert_called_once_with(success=False)
    for callback in scheduled:
        callback(0)
    root.show_message_popup.assert_called_once()
    assert "Upload file error" in root.show_message_popup.call_args.args[0]
    assert any("can't start new thread" in line for line in _logged(root))


def test_upload_start_marks_the_link_busy_until_the_thread_runs(tmp_path, scheduled, monkeypatch):
    """The busy mark is still set before the worker starts, so nothing else
    takes the link in between."""
    seen = []

    class _RecordingThread(_ImmediateThread):
        def start(self):
            seen.append(root.controller.sendNUM)

    monkeypatch.setattr("carveracontroller.main.threading.Thread", _RecordingThread)
    root = _host(tmp_path, tmp_path / "cache")
    root.filetype = ""
    job = tmp_path / "job.nc"
    job.write_bytes(b"G0 X0\n")

    Makera.uploadLocalFile(root, str(job))

    assert seen == [SEND_FILE]
