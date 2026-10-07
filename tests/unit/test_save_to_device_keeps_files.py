"""Save to device downloads a machine file into a folder the user chose.

The download, its decompress and both checksums happen away from that
folder; only a complete, verified file is put in place there, in one step.
Nothing else in the folder is touched, and if anything fails the user's
existing file is left exactly as it was and they are told why.
"""

from __future__ import annotations

import os
import stat
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller.main import Makera
from tests.unit.test_open_compressed_file import GCODE, _compress

OLD = b"G21\n(the user's existing copy)\n"
NEIGHBOURS = {
    "job.nc.lz": b"the user's own .lz",
    "job.nc.tmp": b"the user's own .tmp",
    "notes.txt": b"unrelated",
}


class _ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def scheduled(monkeypatch):
    calls = []
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: calls.append(cb))
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    return calls


def _host(tmp_path, payload, result=1, advertised_md5=None):
    """A controller whose machine sends `payload` for any download and
    reports `result` (1 sent, 0 unchanged, None failed)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    modem = SimpleNamespace(deferred_download_md5=None, download_md5_failed=False)

    def download(path, _md5, _callback):
        if result:
            with open(path, "wb") as handle:
                handle.write(payload)
            modem.deferred_download_md5 = advertised_md5
        return result

    root = Makera.__new__(Makera)
    root.temp_dir = str(cache)
    root.downloading_config = False
    root.backing_up_config = False
    root.file_popup = SimpleNamespace(firmware_mode=False, selected_machine_filesize=len(payload))
    root.controller = SimpleNamespace(
        comms=SimpleNamespace(uses_framed_transfer=False),
        downloadCommand=MagicMock(return_value=True),
        pauseStream=MagicMock(),
        resumeStream=MagicMock(),
        stream=SimpleNamespace(download=download, modem=modem),
        log=MagicMock(),
    )
    root.update_recent_remote_dir_list = MagicMock()
    root._ingest_machine_gcode_thumbnail = MagicMock()
    root.show_message_popup = MagicMock()
    return root


def _folder(tmp_path, existing=OLD):
    folder = tmp_path / "chosen"
    folder.mkdir()
    for name, data in NEIGHBOURS.items():
        (folder / name).write_bytes(data)
    if existing is not None:
        (folder / "job.nc").write_bytes(existing)
    return folder


def _save(root, folder):
    Makera.save_machine_file_to_device(root, "/sd/gcodes/job.nc", str(folder / "job.nc"))


def _messages(root, scheduled):
    return [
        cb.args[0] for cb in scheduled if isinstance(cb, partial) and cb.func is root.show_message_popup and cb.args
    ]


def _contents(folder):
    return {name: (folder / name).read_bytes() for name in sorted(os.listdir(folder))}


def test_compressed_download_replaces_the_file_and_touches_nothing_else(tmp_path, scheduled):
    folder = _folder(tmp_path)
    root = _host(tmp_path, _compress(GCODE))

    _save(root, folder)

    assert _contents(folder) == {**NEIGHBOURS, "job.nc": GCODE.encode()}
    assert _messages(root, scheduled) == []
    root._ingest_machine_gcode_thumbnail.assert_called_once_with("/sd/gcodes/job.nc", str(folder / "job.nc"))


def test_plain_download_replaces_the_file_and_touches_nothing_else(tmp_path, scheduled):
    folder = _folder(tmp_path)
    root = _host(tmp_path, GCODE.encode())

    _save(root, folder)

    assert _contents(folder) == {**NEIGHBOURS, "job.nc": GCODE.encode()}


def test_download_that_fails_to_decompress_leaves_the_existing_file_and_says_why(tmp_path, scheduled):
    folder = _folder(tmp_path)
    root = _host(tmp_path, _compress(GCODE, checksum_offset=1))

    _save(root, folder)

    assert _contents(folder) == {**NEIGHBOURS, "job.nc": OLD}
    assert any("could not be decompressed" in message for message in _messages(root, scheduled))


def test_download_whose_md5_does_not_match_leaves_the_existing_file_and_says_why(tmp_path, scheduled):
    folder = _folder(tmp_path)
    root = _host(tmp_path, _compress(GCODE), advertised_md5="0" * 32)

    _save(root, folder)

    assert _contents(folder) == {**NEIGHBOURS, "job.nc": OLD}
    assert any("MD5" in message for message in _messages(root, scheduled))


def test_failed_download_leaves_the_existing_file_and_says_so(tmp_path, scheduled):
    folder = _folder(tmp_path)
    root = _host(tmp_path, b"", result=None)

    _save(root, folder)

    assert _contents(folder) == {**NEIGHBOURS, "job.nc": OLD}
    assert any("Download file error" in message for message in _messages(root, scheduled))


def test_unchanged_file_is_left_as_it_is(tmp_path, scheduled):
    """The machine found the existing file identical and sent nothing."""
    folder = _folder(tmp_path)
    root = _host(tmp_path, b"", result=0)

    _save(root, folder)

    assert _contents(folder) == {**NEIGHBOURS, "job.nc": OLD}


def test_new_file_is_saved_and_nothing_else_is_left_behind(tmp_path, scheduled):
    folder = _folder(tmp_path, existing=None)
    root = _host(tmp_path, _compress(GCODE))

    _save(root, folder)

    assert _contents(folder) == {**NEIGHBOURS, "job.nc": GCODE.encode()}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_new_file_gets_the_folders_usual_permissions(tmp_path, scheduled):
    folder = _folder(tmp_path, existing=None)
    probe = folder / "probe"
    probe.write_bytes(b"")
    usual = stat.S_IMODE(os.stat(probe).st_mode)
    probe.unlink()
    root = _host(tmp_path, _compress(GCODE))

    _save(root, folder)

    assert stat.S_IMODE(os.stat(folder / "job.nc").st_mode) == usual


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_replaced_file_keeps_its_permissions(tmp_path, scheduled):
    folder = _folder(tmp_path)
    os.chmod(folder / "job.nc", 0o640)
    root = _host(tmp_path, _compress(GCODE))

    _save(root, folder)

    assert (folder / "job.nc").read_bytes() == GCODE.encode()
    assert stat.S_IMODE(os.stat(folder / "job.nc").st_mode) == 0o640
