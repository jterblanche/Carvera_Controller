"""Opening a QuickLZ-compressed G-code file replaces it with its decompressed
text, but only once that text is complete and verified. If the decompress
fails, or a checksum does not match, the file the user opened must be left
exactly as it was, with nothing else left behind in its folder.
"""

from __future__ import annotations

import os
import queue
import stat
import struct
from types import SimpleNamespace

import pytest
import quicklz

import carveracontroller.main as main_module
from tests.unit.test_file_load_main_thread import _drain, _Guard, _load_host

GCODE = "G21\nG90\n" + "".join("G1 X%d Y%d F1000\n" % (i, i) for i in range(200))


def _compress(text, checksum_offset=0):
    """The controller's QuickLZ file format: blocks of a big-endian length and
    the compressed block, then a 16-bit sum of the uncompressed bytes."""
    data = text.encode("utf-8")
    out = b""
    for start in range(0, len(data), 4096):
        block = quicklz.compress(data[start : start + 4096])
        out += struct.pack(">I", len(block)) + block
    return out + struct.pack(">H", (sum(data) + checksum_offset) & 0xFFFF)


def _open(monkeypatch, tmp_path, contents, stream=None, mode=None):
    """Write `contents` to a file and open it with the real load_gcode_file,
    then run what it scheduled for the main thread. Returns the host and the
    file's path."""
    guard = _Guard()
    scheduled = queue.Queue()
    root = _load_host(monkeypatch, guard, scheduled)
    root.controller.stream = stream
    root.loading_file = False
    root._open_guard = guard
    path = tmp_path / "job.nc"
    path.write_bytes(contents)
    if mode is not None:
        os.chmod(path, mode)
    root.load_gcode_file(str(path))
    _drain(scheduled)
    return root, path


def _folder(tmp_path):
    return sorted(os.listdir(tmp_path))


def test_compressed_file_that_fails_to_decompress_is_left_in_place(monkeypatch, tmp_path):
    def broken(_block):
        raise ValueError("corrupt block")

    monkeypatch.setattr(main_module.quicklz, "decompress", broken)
    original = _compress(GCODE)

    root, path = _open(monkeypatch, tmp_path, original)

    assert path.read_bytes() == original
    assert _folder(tmp_path) == [".lz", "job.nc"]
    assert "could not be decompressed" in root.message_popup.lb_content.text


def test_compressed_file_whose_checksum_does_not_match_is_left_in_place(monkeypatch, tmp_path):
    original = _compress(GCODE, checksum_offset=1)

    root, path = _open(monkeypatch, tmp_path, original)

    assert path.read_bytes() == original
    assert _folder(tmp_path) == [".lz", "job.nc"]
    assert "could not be decompressed" in root.message_popup.lb_content.text


def test_compressed_download_whose_md5_does_not_match_is_left_in_place(monkeypatch, tmp_path):
    """The machine advertised the MD5 of the uncompressed file, and the
    decompressed text does not match it."""
    original = _compress(GCODE)
    stream = SimpleNamespace(modem=SimpleNamespace(deferred_download_md5="0" * 32))

    root, path = _open(monkeypatch, tmp_path, original, stream=stream)

    assert path.read_bytes() == original
    assert _folder(tmp_path) == [".lz", "job.nc"]
    assert "MD5" in root.message_popup.lb_content.text


def test_compressed_file_that_decompresses_is_replaced_by_its_text_and_loads(monkeypatch, tmp_path):
    original = _compress(GCODE)

    root, path = _open(monkeypatch, tmp_path, original)

    assert path.read_text(encoding="utf-8") == GCODE
    assert (tmp_path / ".lz" / "job.nc.lz").read_bytes() == original
    assert _folder(tmp_path) == [".lz", "job.nc"]
    assert "load_end" in root._open_guard.names()
    assert root.lines[0] == "G21\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_decompressed_file_keeps_the_original_file_permissions(monkeypatch, tmp_path):
    _root, path = _open(monkeypatch, tmp_path, _compress(GCODE), mode=0o664)

    assert path.read_text(encoding="utf-8") == GCODE
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o664
