"""Every way out of an upload leaves the link marked free.

uploadLocalFile sets ``controller.sendNUM`` before it starts the upload
thread, and Makera.doUpload sets ``uploading`` once the transfer is about to
start. While sendNUM is set the controller treats its own transfer as using
the link: it sends no status queries (Controller.viewStatusReport) and keeps
its heartbeat watchdog fed without hearing from the machine. So whatever way
doUpload ends, by returning early, by an error before or after the transfer,
or by the transfer failing, both must be cleared, or the controller stops
asking for status until the next load command happens to clear sendNUM.

These run Makera.doUpload directly with a stand-in link, the way
uploadLocalFile's worker thread does.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller.Controller import SEND_FILE
from carveracontroller.main import Makera


def _host(tmp_path, *, upload=True):
    root = Makera.__new__(Makera)
    root.temp_dir = str(tmp_path / "cache")
    root._uploading_firmware = False
    root.uploading = False
    root.decompstatus = False
    root.file_popup = SimpleNamespace(machine_dir="/sd/gcodes", refresh_machine=MagicMock())
    root.controller = SimpleNamespace(
        sendNUM=SEND_FILE,
        loadNUM=0,
        pauseStream=MagicMock(),
        resumeStream=MagicMock(),
        uploadCommand=MagicMock(return_value=True),
        stream=SimpleNamespace(upload=MagicMock(return_value=upload)),
        log=MagicMock(),
    )
    root.update_recent_local_dir_list = MagicMock()
    root.queue_machine_thumbnail = MagicMock()
    root._cleanup_firmware_temp = MagicMock()
    return root


@pytest.fixture(autouse=True)
def no_kivy_clock(monkeypatch):
    scheduled = []
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda *a, **kw: scheduled.append(a))
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)
    return scheduled


def _upload(root, local_file):
    root.uploading_file = str(local_file)
    root.original_upload_filepath = str(local_file)
    Makera.doUpload(root, None)


def _assert_link_free(root):
    assert root.controller.sendNUM == 0
    assert root.uploading is False


def _job(tmp_path, name="job.nc", content=b"G0 X0\n"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_unreadable_local_file_clears_send_state(tmp_path):
    """The local file is gone by the time the upload thread runs: doUpload
    returns before the transfer, with an error popup."""
    root = _host(tmp_path)

    _upload(root, tmp_path / "missing.nc")

    root.controller.stream.upload.assert_not_called()
    _assert_link_free(root)


def test_error_working_out_the_remote_path_clears_send_state(tmp_path, monkeypatch):
    """Working out where a firmware file goes on the card fails before the
    transfer starts. The error still surfaces, and the link is free."""
    root = _host(tmp_path)
    root._uploading_firmware = True
    monkeypatch.setattr(root, "_firmware_plan", MagicMock(side_effect=ValueError("no plan")), raising=False)

    with pytest.raises(ValueError):
        _upload(root, _job(tmp_path, "firmware.bin"))

    root.controller.stream.upload.assert_not_called()
    _assert_link_free(root)


def test_upload_command_held_back_clears_send_state(tmp_path):
    """The upload command is not sent (the connection is not ready yet)."""
    root = _host(tmp_path)
    root.controller.uploadCommand.return_value = False

    _upload(root, _job(tmp_path))

    root.controller.stream.upload.assert_not_called()
    _assert_link_free(root)


def test_transfer_error_clears_send_state(tmp_path):
    root = _host(tmp_path)
    root.controller.stream.upload.side_effect = OSError("link dropped")

    _upload(root, _job(tmp_path))

    _assert_link_free(root)


def test_cancelled_transfer_clears_send_state(tmp_path):
    root = _host(tmp_path, upload=None)

    _upload(root, _job(tmp_path))

    _assert_link_free(root)


def test_failed_compressed_upload_whose_file_is_already_gone_clears_send_state(tmp_path):
    """A failed compressed upload removes its ``.lz`` file afterwards. When
    that file is already gone the removal raises; the link is still free."""
    root = _host(tmp_path, upload=False)
    _job(tmp_path)
    compressed = _job(tmp_path, "job.nc.lz", b"compressed bytes")

    def upload_and_lose_file(*args, **kwargs):
        compressed.unlink()
        return False

    root.controller.stream.upload.side_effect = upload_and_lose_file

    with pytest.raises(FileNotFoundError):
        _upload(root, compressed)

    _assert_link_free(root)


def test_error_keeping_a_local_copy_after_a_good_transfer_clears_send_state(tmp_path):
    """The transfer succeeds but keeping a local copy of the file fails (the
    cache directory cannot be created). The link is still free."""
    root = _host(tmp_path)
    (tmp_path / "cache").write_bytes(b"a file where the cache directory should be")

    with pytest.raises(OSError):
        _upload(root, _job(tmp_path))

    _assert_link_free(root)


def test_successful_upload_clears_send_state(tmp_path):
    root = _host(tmp_path)

    _upload(root, _job(tmp_path))

    root.controller.stream.upload.assert_called_once()
    _assert_link_free(root)
