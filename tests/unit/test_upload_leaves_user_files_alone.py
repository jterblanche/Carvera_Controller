"""An upload never writes, overwrites or deletes anything in the user's
folders, however it ends.

When the machine accepts compressed uploads, the compressed copy (and the
uncompressed copy beside it that the upload's MD5 is taken from) are made in
a folder of their own inside the controller's temp folder, and only that
folder's files are removed afterwards. A file of the user's that happens to
be called <name>.lz, beside the job or as the job itself, is left alone.
"""

from __future__ import annotations

import os

import pytest

from carveracontroller import Utils
from carveracontroller.main import Makera
from tests.unit.test_upload_compress_failure import _host, _ImmediateThread

OUTCOMES = {"succeeds": True, "fails": False, "is cancelled": None}


@pytest.fixture
def scheduled(monkeypatch):
    calls = []
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: calls.append(cb))
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)
    monkeypatch.setattr("carveracontroller.main.threading.Thread", _ImmediateThread)
    return calls


def _user_folder(tmp_path):
    folder = tmp_path / "jobs"
    folder.mkdir()
    job = folder / "job.nc"
    job.write_bytes(b"G21\nG0 X0\nG0 X1\n" * 50)
    return folder, job


def _snapshot(folder):
    return {name: (folder / name).read_bytes() for name in sorted(os.listdir(folder))}


@pytest.mark.parametrize("outcome", OUTCOMES, ids=list(OUTCOMES))
def test_compressed_upload_leaves_the_users_own_lz_beside_the_job_alone(tmp_path, scheduled, outcome):
    folder, job = _user_folder(tmp_path)
    (folder / "job.nc.lz").write_bytes(b"the user's own file")
    before = _snapshot(folder)
    root = _host(tmp_path, tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    root.controller.stream.upload.return_value = OUTCOMES[outcome]

    Makera.uploadLocalFile(root, str(job))

    assert _snapshot(folder) == before


@pytest.mark.parametrize("outcome", OUTCOMES, ids=list(OUTCOMES))
def test_compressed_copy_is_made_in_a_folder_of_its_own_and_removed(tmp_path, scheduled, outcome):
    folder, job = _user_folder(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    root = _host(tmp_path, cache)
    root.controller.stream.upload.return_value = OUTCOMES[outcome]

    Makera.uploadLocalFile(root, str(job))

    sent, md5 = root.controller.stream.upload.call_args.args[:2]
    assert os.path.basename(sent) == "job.nc.lz"  # the machine strips .lz when it unpacks
    assert md5 == Utils.md5(str(job))  # the MD5 is of the uncompressed file
    assert os.path.dirname(os.path.dirname(sent)) == str(cache)
    assert not os.path.exists(os.path.dirname(sent))
    assert sorted(os.listdir(folder)) == ["job.nc"]


def test_successful_compressed_upload_still_keeps_a_cache_copy_of_the_job(tmp_path, scheduled):
    folder, job = _user_folder(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    root = _host(tmp_path, cache)

    Makera.uploadLocalFile(root, str(job))

    assert (cache / "gcodes" / "job.nc").read_bytes() == job.read_bytes()
    assert (cache / "gcodes" / ".lz" / "job.nc.lz").exists()


def test_failed_uncompressed_upload_of_a_file_named_lz_does_not_delete_it(tmp_path, scheduled):
    """The machine does not take compressed uploads, so the user's own
    part.lz is sent as it is. (It fails before the transfer: the upload
    takes its MD5 from the name without .lz.)"""
    folder = tmp_path / "jobs"
    folder.mkdir()
    job = folder / "part.lz"
    job.write_bytes(b"the user's own file")
    root = _host(tmp_path, tmp_path / "cache")
    root.filetype = ""

    Makera.uploadLocalFile(root, str(job))

    assert job.read_bytes() == b"the user's own file"
