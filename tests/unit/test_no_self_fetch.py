"""A controller never auto-fetches a file it has just uploaded itself.

The machine announces every finished upload to every identified client,
the uploader included. The uploader must recognise that announcement as
the echo of its own upload -- by the path it sent and the MD5 it announced
for it, not by whether it currently believes it holds control -- or it
sends ``download`` for the file it already has, and that download collides
on the link with the listing refresh the upload itself starts. A different
client's later upload of the same name with different contents is still a
new file, and is still fetched.

These drive Makera.doUpload (carveracontroller/main.py) with a stand-in
link, then deliver the machine's upload-finished event to
on_passive_file_published exactly as Controller._notify_file_published
does, and watch whether a download is started.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller.machine.passive_fetch import PassiveFetchTracker
from carveracontroller.main import Makera


class _ImmediateThread:
    """Runs the target synchronously on .start(), so a fetch that
    _check_passive_fetch starts is visible to the test at once."""

    def __init__(self, target=None, args=(), **kwargs):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _host(tmp_path, *, has_control=False):
    root = Makera.__new__(Makera)
    root.temp_dir = str(tmp_path / "cache")
    root._passive_fetch = PassiveFetchTracker()
    root._auto_fetch_in_progress = False
    root._uploading_firmware = False
    root.file_popup = SimpleNamespace(machine_dir="/sd/gcodes", refresh_machine=MagicMock())
    root.controller = SimpleNamespace(
        has_control=has_control,
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
    root.doDownload = MagicMock(return_value=1)
    root.load_gcode_file = MagicMock()
    return root


@pytest.fixture
def app(monkeypatch):
    """The running app, with some other file on screen and the machine
    idle -- the state in which a passive fetch starts at once."""
    running = SimpleNamespace(
        selected_remote_filename="/sd/gcodes/other.nc",
        selected_local_filename="/tmp/other.nc",
        state="Idle",
    )
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: running)
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda *a, **kw: None)
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    return running


def _local_file(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _upload(root, local_file):
    """Run doUpload for `local_file`, the way uploadLocalFile's worker
    thread does."""
    root.uploading_file = str(local_file)
    root.original_upload_filepath = str(local_file)
    Makera.doUpload(root, None)


def test_own_upload_echo_is_not_fetched(tmp_path, app):
    """The uploader does not believe it holds control, and the machine's
    upload-finished event for its own file reaches it straight after the
    transfer. It must not download that file back, and must leave what is
    on screen alone."""
    root = _host(tmp_path)
    content = b"G0 X0\nG0 X1\n"
    local = _local_file(tmp_path, "job.nc", content)

    _upload(root, local)
    Makera.on_passive_file_published(root, "/sd/gcodes/job.nc", hashlib.md5(content).digest())

    root.doDownload.assert_not_called()
    assert root._passive_fetch.pending_path is None
    assert app.selected_remote_filename == "/sd/gcodes/other.nc"
    assert app.selected_local_filename == "/tmp/other.nc"


def test_own_upload_echo_dispatched_at_the_end_of_the_transfer_is_not_fetched(tmp_path, app):
    """The event can be read off the link by the transfer itself and handed
    on as the link is given back (Controller.resumeStream), before doUpload
    has returned. The upload is already recognised as this controller's own
    by then."""
    root = _host(tmp_path)
    content = b"G0 X2\n"
    local = _local_file(tmp_path, "job.nc", content)
    root.controller.resumeStream = MagicMock(
        side_effect=lambda: Makera.on_passive_file_published(root, "/sd/gcodes/job.nc", hashlib.md5(content).digest())
    )

    _upload(root, local)

    root.controller.resumeStream.assert_called()
    root.doDownload.assert_not_called()
    assert root._passive_fetch.pending_path is None


def test_own_compressed_upload_echo_is_not_fetched(tmp_path, app):
    """A compressed upload sends `<name>.lz`, announcing the MD5 of the
    uncompressed file; the machine unpacks it and announces the unpacked
    name. The echo is still recognised as this controller's own."""
    root = _host(tmp_path)
    content = b"G0 X3\nG0 X4\n"
    _local_file(tmp_path, "job.nc", content)
    compressed = _local_file(tmp_path, "job.nc.lz", b"compressed bytes")

    _upload(root, compressed)
    Makera.on_passive_file_published(root, "/sd/gcodes/job.nc", hashlib.md5(content).digest())

    root.doDownload.assert_not_called()
    assert root._passive_fetch.pending_path is None


def test_another_controllers_upload_of_the_same_name_is_still_fetched(tmp_path, app):
    """After this controller's own upload of job.nc, another controller
    uploads a different job.nc. Its digest differs from the one this
    controller sent, so it is a new file and is fetched."""
    root = _host(tmp_path)
    content = b"G0 X5\n"
    local = _local_file(tmp_path, "job.nc", content)

    _upload(root, local)
    Makera.on_passive_file_published(root, "/sd/gcodes/job.nc", hashlib.md5(content).digest())
    root.doDownload.assert_not_called()

    Makera.on_passive_file_published(root, "/sd/gcodes/job.nc", hashlib.md5(b"G0 Y9\n").digest())

    root.doDownload.assert_called_once()
    assert root.doDownload.call_args.args[0] == "/sd/gcodes/job.nc"


def test_another_controllers_upload_of_a_different_file_is_still_fetched(tmp_path, app):
    root = _host(tmp_path)
    content = b"G0 X6\n"
    local = _local_file(tmp_path, "job.nc", content)

    _upload(root, local)
    Makera.on_passive_file_published(root, "/sd/gcodes/theirs.nc", hashlib.md5(content).digest())

    root.doDownload.assert_called_once()
    assert root.doDownload.call_args.args[0] == "/sd/gcodes/theirs.nc"


def test_playing_own_upload_after_selecting_it_is_not_fetched(tmp_path, app):
    """Upload-and-select, then play: the play-started event carries no
    digest of its own. The digest this controller sent for that path still
    applies, so the matching local copy on screen is not fetched again."""
    root = _host(tmp_path)
    content = b"G0 X7\n"
    local = _local_file(tmp_path, "job.nc", content)

    _upload(root, local)
    Makera.on_passive_file_published(root, "/sd/gcodes/job.nc", hashlib.md5(content).digest())
    app.selected_remote_filename = "/sd/gcodes/job.nc"
    app.selected_local_filename = str(local)
    Makera.on_passive_file_published(root, "/sd/gcodes/job.nc")

    root.doDownload.assert_not_called()


def test_a_remembered_digest_does_not_outlive_the_connection(tmp_path, app):
    """A digest learnt on one connection says nothing about the card after a
    reconnect: uploads may have happened in between. A fresh connection
    starts with a fresh tracker (updateStatus's disconnect handling), and a
    play-started event for the same path is then fetched, not skipped
    against the old digest."""
    root = _host(tmp_path)
    content = b"G0 X8\n"
    local = _local_file(tmp_path, "job.nc", content)
    app.selected_remote_filename = "/sd/gcodes/job.nc"
    app.selected_local_filename = str(local)
    Makera.on_passive_file_published(root, "/sd/gcodes/job.nc", hashlib.md5(content).digest())
    root.doDownload.assert_not_called()  # matching local copy already on screen

    root._passive_fetch = PassiveFetchTracker()  # what a reconnect does
    Makera.on_passive_file_published(root, "/sd/gcodes/job.nc")

    root.doDownload.assert_called_once()
