"""How many points the parser makes for a straight move.

A straight move used to become one point every half millimetre. Only the
first step and the end are kept now: the points between lay on the same
line with the same line number, feed, tool and colour, so the drawn path
is the same. A move that turns A keeps every step, because around the A
axis the drawn path curves."""

import math

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.GcodeViewer import _compute_line_times_worker


def _parse(lines):
    cnc = CNC()
    cnc.init()
    for number, line in enumerate(lines, 1):
        cnc.parseLine(line, number)
    return cnc.coordinates


def _points_of_line(coordinates, line_no):
    return [p for p in coordinates if p[5] == line_no]


def _full_interpolation(start, end, steps):
    """The points every step of a move would have given."""
    return [tuple(s + i / steps * (e - s) for s, e in zip(start, end)) for i in range(1, steps + 1)]


def test_long_straight_move_keeps_the_first_step_and_the_end():
    coords = _parse(["G21 G90", "G1 X0 Y0 Z0 F500", "G1 X10 Y0 Z0 F500"])

    points = _points_of_line(coords, 3)
    assert [tuple(p[:3]) for p in points] == [pytest.approx((0.5, 0.0, 0.0)), pytest.approx((10.0, 0.0, 0.0))]


def test_kept_points_are_exactly_those_full_interpolation_gives():
    coords = _parse(["G21 G90", "G1 X1 Y2 Z3 F500", "G1 X8.3 Y-4.1 Z5.75 F700"])

    points = _points_of_line(coords, 3)
    full = _full_interpolation((1.0, 2.0, 3.0), (8.3, -4.1, 5.75), int(7.3 / 0.5))
    assert [tuple(p[:3]) for p in points] == [full[0][:3], full[-1][:3]]


def test_left_out_points_lie_on_the_drawn_segment_with_the_same_attributes():
    """Drawing first-step -> end is the same picture as drawing every step:
    each left-out point sits on that segment and would carry the same line
    number, feed, tool and colour as the end point."""
    coords = _parse(["G21 G90", "G1 X0 Y0 Z0 F500", "G0 X3 Y4 Z12"])
    first, end = _points_of_line(coords, 3)
    assert first[4:] == end[4:]

    full = _full_interpolation((0.0, 0.0, 0.0), (3.0, 4.0, 12.0), int(12 / 0.5))
    span = [end[i] - first[i] for i in range(3)]
    span_len = math.sqrt(sum(c * c for c in span))
    for point in full[1:-1]:
        offset = [point[i] - first[i] for i in range(3)]
        t = sum(offset[i] * span[i] for i in range(3)) / (span_len * span_len)
        assert 0.0 < t < 1.0
        assert [first[i] + t * span[i] for i in range(3)] == pytest.approx(list(point[:3]))


def test_short_straight_move_is_one_point():
    coords = _parse(["G21 G90", "G1 X0 Y0 Z0 F500", "G1 X0.4 F500"])
    assert len(_points_of_line(coords, 3)) == 1


def test_move_of_two_steps_keeps_both():
    coords = _parse(["G21 G90", "G1 X0 Y0 Z0 F500", "G1 X1.0 F500"])
    assert [p[0] for p in _points_of_line(coords, 3)] == pytest.approx([0.5, 1.0])


def test_move_that_turns_a_keeps_every_step():
    coords = _parse(["G21 G90", "G0 X0 Y0 Z10 A0", "G1 A90 F1500"])
    points = _points_of_line(coords, 3)
    assert len(points) == int(90 / 0.5)
    steps = [abs(b[3] - a[3]) for a, b in zip(points, points[1:])]
    assert steps == pytest.approx([0.5] * (len(points) - 1))


def test_run_time_of_a_straight_move_is_unchanged():
    """The estimate sums segment times; leaving out collinear points must
    not change the total."""
    coords = _parse(["G21 G90", "M3 S10000", "G1 X0 Y0 Z0 F600", "G1 X50 Y20 Z-3 F600", "G0 X0 Y0 Z5"])
    raw = []
    for p in coords:
        raw.extend(p[:3])
    line_nos = [p[5] for p in coords]
    # As the viewer stores them: 0 for a rapid, the feed for a cut.
    feeds = [float(p[7]) if p[4] else 0.0 for p in coords]
    assert feeds[1] == 600.0 and feeds[-1] == 0.0
    times = _compute_line_times_worker(raw, line_nos, feeds, None, 0, [p[3] for p in coords])

    # Every step, as before. (The first move, to the origin the parser
    # starts at, gives no point, so the path starts at line 4's first step.)
    full = _full_interpolation((0.0, 0.0, 0.0), (50.0, 20.0, -3.0), 100)
    full += _full_interpolation((50.0, 20.0, -3.0), (0.0, 0.0, 5.0), 100)
    assert tuple(coords[0][:3]) == full[0]
    full_raw = [c for point in full for c in point]
    full_line_nos = [4] * 100 + [5] * 100
    full_feeds = [600.0] * 100 + [0.0] * 100
    expected = _compute_line_times_worker(full_raw, full_line_nos, full_feeds, None, 0, [0.0] * 200)
    assert times[-1] == pytest.approx(expected[-1])
