"""The toolpath point budget: how many points a file may need before the
controller loads it without drawing its toolpath."""

from carveracontroller import toolpath_budget as tb

MIB = 1024 * 1024


def _reader(files):
    return lambda path: files.get(path)


def test_budget_scales_with_the_free_memory():
    free = 4 * 1024 * MIB
    assert tb.point_budget(free) == int(free * tb.MEMORY_SHARE / tb.BYTES_PER_POINT)
    assert tb.point_budget(2 * free) > tb.point_budget(free)


def test_budget_never_drops_below_the_minimum():
    assert tb.point_budget(0) == tb.MIN_POINTS
    assert tb.point_budget(10 * MIB) == tb.MIN_POINTS


def test_budget_falls_back_when_the_free_memory_is_unknown():
    assert tb.point_budget(None) == tb.DEFAULT_POINTS


def test_over_budget_only_past_the_budget():
    assert not tb.over_budget(999, 1000)
    assert not tb.over_budget(1000, 1000)
    assert tb.over_budget(1001, 1000)


def test_reads_mem_available_from_meminfo():
    meminfo = "MemTotal:       16000000 kB\nMemFree:  100 kB\nMemAvailable:    2048 kB\n"
    assert tb._meminfo_available(meminfo) == 2048 * 1024
    assert tb._meminfo_available("MemTotal: 1 kB\n") is None
    assert tb._meminfo_available(None) is None


def test_cgroup_headroom_is_the_room_left_under_the_limit():
    assert tb._cgroup_headroom("1073741824\n", "473741824\n") == 600000000
    assert tb._cgroup_headroom("100\n", "200\n") == 0


def test_cgroup_file_cache_the_kernel_can_reclaim_is_not_counted_as_used():
    stat = "anon 300000000\nfile 200000000\ninactive_file 150000000\nactive_file 50000000\n"
    assert tb._cgroup_headroom("1000000000", "500000000", stat) == 650000000
    # Never more than the usage itself.
    assert tb._cgroup_headroom("1000", "100", "inactive_file 500\n") == 1000
    # cgroup v1 names it differently.
    stat_v1 = "cache 9\ntotal_inactive_file 100\n"
    assert tb._cgroup_headroom("1000", "600", stat_v1, reclaimable_key="total_inactive_file") == 500


def test_cgroup_without_a_limit_gives_no_reading():
    assert tb._cgroup_headroom("max\n", "1000\n") is None
    assert tb._cgroup_headroom(str(1 << 62), "1000") is None  # cgroup v1's "unlimited"
    assert tb._cgroup_headroom(None, "1000") is None
    assert tb._cgroup_headroom("junk", "1000") is None


def test_linux_takes_the_lower_of_the_computer_and_its_container():
    files = {
        "/proc/meminfo": "MemAvailable: %d kB\n" % (8 * 1024 * 1024),  # 8 GiB free on the computer
        "/sys/fs/cgroup/memory.max": str(2048 * MIB),
        "/sys/fs/cgroup/memory.current": str(500 * MIB),
        "/sys/fs/cgroup/memory.stat": "inactive_file %d\n" % (100 * MIB),
    }
    assert tb.available_memory_bytes("linux", _reader(files)) == 1648 * MIB


def test_linux_outside_a_container_uses_mem_available():
    files = {"/proc/meminfo": "MemAvailable: 1024 kB\n", "/sys/fs/cgroup/memory.max": "max\n"}
    assert tb.available_memory_bytes("linux", _reader(files)) == 1024 * 1024


def test_linux_cgroup_v1_limit_counts_too():
    files = {
        "/proc/meminfo": "MemAvailable: %d kB\n" % (8 * 1024 * 1024),
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(1024 * MIB),
        "/sys/fs/cgroup/memory/memory.usage_in_bytes": str(24 * MIB),
    }
    assert tb.available_memory_bytes("linux", _reader(files)) == 1000 * MIB


def test_unknown_when_nothing_can_be_read():
    assert tb.available_memory_bytes("linux", _reader({})) is None
    assert tb.available_memory_bytes("darwin", _reader({"/proc/meminfo": "MemAvailable: 1 kB\n"})) is None


def test_measured_test_files_against_container_limits():
    """The run sheet's files (points measured with the controller): with a
    controller idling at about 450 MB, a 2 GB container draws the 20 MB
    file, and a 1 GB one draws the 5 MB file but not the 20 MB one."""
    points_5mb, points_20mb = 830_401, 3_322_161
    in_2_gib = tb.point_budget((2048 - 450) * MIB)
    in_1_gib = tb.point_budget((1024 - 450) * MIB)
    assert not tb.over_budget(points_20mb, in_2_gib)
    assert not tb.over_budget(points_5mb, in_1_gib)
    assert tb.over_budget(points_20mb, in_1_gib)
