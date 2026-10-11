"""How many toolpath points this computer can afford to draw.

Drawing a file costs memory for every point of its toolpath: the viewer's
per-point arrays, the drawn meshes (which Kivy and the graphics driver
each keep a copy of) and the run-time estimate. A file with millions of
moves can need more memory than the computer has free, and a controller
that runs out of memory is killed outright. That matters most for a
controller that draws a file someone else uploaded (it fetches and draws
the job file without being asked), so before a file is drawn the
controller works out a point budget from the memory it has free, and a
file whose toolpath goes over the budget is loaded without its toolpath
instead (the file can still be run).

Kivy-free and with the memory readings injectable, so the decisions are
testable without a UI or a particular computer.
"""

from __future__ import annotations

import os
import sys

# Memory a drawn point costs at the peak of a load, the file's text
# included: measured at 255 to 330 bytes (3.3 million points from a 20 MB
# file, 0.8 million from a 5 MB one), rounded up.
BYTES_PER_POINT = 350

# Share of the free memory a load may plan to use. The rest is margin for
# files that cost more per point and for whatever else the controller is
# doing meanwhile.
MEMORY_SHARE = 0.75

# Always draw a toolpath of up to this many points (a 5 MB file of short
# moves is about that), whatever the reading says: a reading can be low
# for a moment, and refusing an ordinary file would be worse than the risk.
MIN_POINTS = 1_000_000

# Budget when the free memory cannot be read (a platform with none of the
# sources below).
DEFAULT_POINTS = 10_000_000

# A cgroup limit at or above this means "no limit".
_NO_LIMIT = 1 << 60


def point_budget(available_bytes):
    """The most toolpath points to draw, given `available_bytes` of free
    memory (None when it could not be read)."""
    if available_bytes is None:
        return DEFAULT_POINTS
    points = int(available_bytes * MEMORY_SHARE / BYTES_PER_POINT)
    return max(MIN_POINTS, points)


def over_budget(points, budget):
    """True once a toolpath of `points` points is too large to draw."""
    return points > budget


def _read_text(path):
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def _meminfo_available(meminfo_text):
    """MemAvailable from /proc/meminfo's text, in bytes, or None."""
    if not meminfo_text:
        return None
    for line in meminfo_text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def _stat_value(stat_text, key):
    """One value from a cgroup memory.stat file's text, or 0."""
    for line in (stat_text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            try:
                return int(parts[1])
            except ValueError:
                return 0
    return 0


def _cgroup_headroom(limit_text, usage_text, stat_text=None, reclaimable_key="inactive_file"):
    """Bytes left under a cgroup memory limit, or None if there is no limit
    or it cannot be read. The cgroup's usage counts file cache the kernel
    gives back under pressure (the file just read, for one); that part,
    ``reclaimable_key`` in memory.stat, is not counted as used."""
    if limit_text is None or usage_text is None:
        return None
    limit_text = limit_text.strip()
    if limit_text == "max":
        return None
    try:
        limit = int(limit_text)
        usage = int(usage_text.strip())
    except ValueError:
        return None
    if limit >= _NO_LIMIT:
        return None
    usage -= min(usage, _stat_value(stat_text, reclaimable_key))
    return max(0, limit - usage)


def _linux_available(read):
    candidates = [
        _meminfo_available(read("/proc/meminfo")),
        # cgroup v2, then v1: a container's limit is lower than what the
        # whole computer reports as free.
        _cgroup_headroom(
            read("/sys/fs/cgroup/memory.max"),
            read("/sys/fs/cgroup/memory.current"),
            read("/sys/fs/cgroup/memory.stat"),
        ),
        _cgroup_headroom(
            read("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            read("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
            read("/sys/fs/cgroup/memory/memory.stat"),
            reclaimable_key="total_inactive_file",
        ),
    ]
    found = [value for value in candidates if value is not None]
    return min(found) if found else None


def _windows_available():
    try:
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys)
    except Exception:
        return None


def available_memory_bytes(platform=None, read=_read_text):
    """Memory this process can still use, in bytes, or None if unknown.

    On Linux (Android included) the lower of the computer's MemAvailable
    and the room left under a cgroup limit, if the controller runs in a
    container. On Windows the free physical memory. Elsewhere (macOS, iOS)
    None: there is no cheap, reliable reading there, and the budget falls
    back to DEFAULT_POINTS."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        return _linux_available(read)
    if platform == "win32" and os.name == "nt":
        return _windows_available()
    return None
