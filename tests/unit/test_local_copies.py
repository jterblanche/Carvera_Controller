"""Pure tests for LocalCopyStore (machine/local_copies.py): finding a local
copy of a job file by its size and MD5."""

from __future__ import annotations

import hashlib
import os

from carveracontroller.machine.local_copies import LocalCopyStore, file_md5


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def test_finds_a_remembered_file_by_size_and_md5(tmp_path):
    content = b"G0 X0\nG1 X10\n"
    path = _write(tmp_path, "part.nc", content)
    store = LocalCopyStore()
    store.remember(path)
    assert store.find(len(content), hashlib.md5(content).digest()) == os.path.abspath(path)


def test_finds_it_under_another_name(tmp_path):
    """The machine names the file by its path on the card; the local copy
    can have any name."""
    content = b"G0 X1\n"
    path = _write(tmp_path, "downloaded-earlier.nc", content)
    store = LocalCopyStore()
    store.remember(path)
    assert store.find(len(content), hashlib.md5(content).digest()) is not None


def test_no_match_without_size_or_checksum(tmp_path):
    content = b"G0 X2\n"
    store = LocalCopyStore()
    store.remember(_write(tmp_path, "a.nc", content))
    assert store.find(None, hashlib.md5(content).digest()) is None
    assert store.find(len(content), b"") is None


def test_a_different_size_or_digest_is_no_match(tmp_path):
    content = b"G0 X3\n"
    store = LocalCopyStore()
    store.remember(_write(tmp_path, "a.nc", content))
    assert store.find(len(content) + 1, hashlib.md5(content).digest()) is None
    assert store.find(len(content), hashlib.md5(b"G0 X4\n").digest()) is None


def test_a_file_changed_since_it_was_remembered_is_no_match(tmp_path):
    content = b"G0 X5\n"
    path = _write(tmp_path, "a.nc", content)
    store = LocalCopyStore()
    store.remember(path)
    assert store.find(len(content), hashlib.md5(content).digest()) is not None
    with open(path, "wb") as f:
        f.write(b"G0 X6\n")  # same size, different content
    os.utime(path, ns=(1, 1))
    assert store.find(len(content), hashlib.md5(content).digest()) is None


def test_a_deleted_file_is_no_match(tmp_path):
    content = b"G0 X7\n"
    path = _write(tmp_path, "a.nc", content)
    store = LocalCopyStore()
    store.remember(path)
    os.remove(path)
    assert store.find(len(content), hashlib.md5(content).digest()) is None


def test_an_unchanged_file_is_hashed_once(tmp_path):
    content = b"G0 X8\n"
    path = _write(tmp_path, "a.nc", content)
    calls = []

    def counting_md5(p):
        calls.append(p)
        return file_md5(p)

    store = LocalCopyStore(md5=counting_md5)
    store.remember(path)
    digest = hashlib.md5(content).digest()
    assert store.find(len(content), digest) is not None
    assert store.find(len(content), digest) is not None
    assert len(calls) == 1


def test_files_of_another_size_are_never_hashed(tmp_path):
    calls = []
    store = LocalCopyStore(md5=lambda p: calls.append(p) or file_md5(p))
    store.remember(_write(tmp_path, "big.nc", b"G0 X9\n" * 100))
    store.find(6, hashlib.md5(b"G0 X9\n").digest())
    assert calls == []


def test_matches_checks_one_path(tmp_path):
    content = b"G0 Y1\n"
    path = _write(tmp_path, "on-screen.nc", content)
    store = LocalCopyStore()
    assert store.matches(path, len(content), hashlib.md5(content).digest())
    assert not store.matches(path, len(content), hashlib.md5(b"other").digest())
    assert not store.matches("", len(content), hashlib.md5(content).digest())


def test_oldest_is_forgotten_beyond_the_limit(tmp_path):
    store = LocalCopyStore(max_remembered=2)
    first = b"G0 Z1\n"
    store.remember(_write(tmp_path, "1.nc", first))
    store.remember(_write(tmp_path, "2.nc", b"G0 Z2\n"))
    store.remember(_write(tmp_path, "3.nc", b"G0 Z3\n"))
    assert store.find(len(first), hashlib.md5(first).digest()) is None
