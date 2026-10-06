"""Local copies of job files, found by content.

The machine names a job's file by its path on the card, its size and the
MD5 of its content. A controller may already hold the same content under
another name or in another folder: a file it uploaded, opened or fetched
earlier. This store remembers the local paths of those files and finds one
whose size and MD5 match, so the toolpath can be drawn without fetching the
file again.

Every match is checked against the file as it is on disk at the time of
the lookup, never against what it was when it was remembered: a file that
has since changed or gone is simply not a match. The MD5 of a file is
cached against its size and modification time, so a file that has not
changed is hashed only once.

Pure, no Kivy: the caller decides when to remember a file and what to do
with a match. Safe to call from any thread.
"""

from __future__ import annotations

import hashlib
import os
import threading

# The most files remembered. The oldest is forgotten first. A lookup stats
# every remembered file of the right size and hashes the ones it has not
# hashed before, so the store is kept small.
MAX_REMEMBERED = 64


def file_md5(path: str) -> str:
    """The MD5 of the file at `path`, as lowercase hex."""
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LocalCopyStore:
    """Local paths of job files, looked up by size and MD5."""

    def __init__(self, md5=file_md5, max_remembered: int = MAX_REMEMBERED) -> None:
        self._md5 = md5
        self._max = max_remembered
        self._lock = threading.Lock()
        # Most recently remembered last.
        self._paths: list[str] = []
        # path -> (size, mtime_ns, md5 hex) of the content last hashed.
        self._digests: dict[str, tuple[int, int, str]] = {}

    def remember(self, path: str) -> None:
        """Remember the file at `path` as a local copy. A blank path is
        ignored. Nothing is read here; the file is hashed on the first
        lookup that needs it."""
        if not path:
            return
        path = os.path.abspath(path)
        with self._lock:
            if path in self._paths:
                self._paths.remove(path)
            self._paths.append(path)
            while len(self._paths) > self._max:
                dropped = self._paths.pop(0)
                self._digests.pop(dropped, None)

    def find(self, size: int | None, checksum: bytes) -> str | None:
        """The most recently remembered file whose content has exactly
        `size` bytes and the MD5 `checksum` (16 raw bytes), or None. Without
        a size or a checksum nothing matches: a name alone, or a size alone,
        never proves two files are the same."""
        if size is None or len(checksum) != 16:
            return None
        with self._lock:
            candidates = list(reversed(self._paths))
        for path in candidates:
            if self._content_matches(path, size, checksum):
                return path
        return None

    def matches(self, path: str, size: int | None, checksum: bytes) -> bool:
        """True if the file at `path` (remembered or not) has exactly `size`
        bytes and the MD5 `checksum` right now."""
        if not path or size is None or len(checksum) != 16:
            return False
        return self._content_matches(os.path.abspath(path), size, checksum)

    def _content_matches(self, path: str, size: int, checksum: bytes) -> bool:
        try:
            stat = os.stat(path)
        except OSError:
            return False
        if stat.st_size != size:
            return False
        with self._lock:
            cached = self._digests.get(path)
        if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            digest = cached[2]
        else:
            try:
                digest = self._md5(path).lower()
            except OSError:
                return False
            with self._lock:
                self._digests[path] = (stat.st_size, stat.st_mtime_ns, digest)
        return digest == checksum.hex()
