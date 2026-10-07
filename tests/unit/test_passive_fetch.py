"""Pure decision tests for PassiveFetchTracker (machine/passive_fetch.py):
when a passive controller should fetch and draw a job file it did not
select itself, versus just showing progress until the machine goes idle."""

from __future__ import annotations

from carveracontroller.machine import passive_fetch
from carveracontroller.machine.passive_fetch import (
    MAX_FETCH_ATTEMPTS,
    RETRY_BACKOFF_S,
    PassiveFetchTracker,
)


def test_nothing_pending_initially():
    t = PassiveFetchTracker()
    assert t.pending_path is None
    assert t.due_fetch(is_idle=True) is None


def test_fetch_is_due_immediately_if_already_idle():
    """upload-finished: the machine is typically idle right after an
    upload, so the fetch fires on the very next observation."""
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    assert t.pending_path == "/sd/job.nc"
    assert t.due_fetch(is_idle=True) == "/sd/job.nc"
    # One-shot: asking again returns nothing until another event arrives.
    assert t.due_fetch(is_idle=True) is None
    assert t.pending_path is None


def test_fetch_waits_while_not_idle():
    """play-started: the machine is usually mid-job, not idle -- the
    fallback the design names: 'shows progress, time and position only
    ... and fetches when the machine is idle again.'"""
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    assert t.due_fetch(is_idle=False) is None
    assert t.pending_path == "/sd/job.nc"  # still owed, not dropped
    assert t.due_fetch(is_idle=False) is None
    # The job finishes and the machine goes idle again.
    assert t.due_fetch(is_idle=True) == "/sd/job.nc"


def test_a_loaded_path_is_not_requeued():
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    t.mark_loaded("/sd/job.nc")
    assert t.pending_path is None
    # A later event for the same path this controller already has must not
    # trigger a redundant fetch.
    t.note_published_file("/sd/job.nc")
    assert t.pending_path is None


def test_a_different_path_after_loading_is_queued():
    t = PassiveFetchTracker()
    t.note_published_file("/sd/first.nc")
    t.mark_loaded("/sd/first.nc")
    t.note_published_file("/sd/second.nc")
    assert t.pending_path == "/sd/second.nc"


def test_mark_loaded_cancels_an_in_flight_pending_request():
    """The caller calls mark_loaded as soon as it starts the fetch, to
    avoid re-triggering while a slow fetch is still running."""
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    t.mark_loaded("/sd/job.nc")  # fetch started
    assert t.due_fetch(is_idle=True) is None


def test_blank_path_is_ignored():
    t = PassiveFetchTracker()
    t.note_published_file("")
    assert t.pending_path is None
    assert t.due_fetch(is_idle=True) is None


def test_a_newer_event_replaces_a_still_pending_older_one():
    t = PassiveFetchTracker()
    t.note_published_file("/sd/first.nc")
    t.note_published_file("/sd/second.nc")
    assert t.due_fetch(is_idle=True) == "/sd/second.nc"


def test_a_failed_fetch_is_not_retried_before_the_backoff_elapses():
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    assert t.due_fetch(is_idle=True, now=0.0) == "/sd/job.nc"
    t.note_fetch_failed("/sd/job.nc", now=0.0)
    # Still owed, but not yet -- too soon after the failure.
    assert t.pending_path == "/sd/job.nc"
    assert t.due_fetch(is_idle=True, now=1.0) is None
    assert t.pending_path == "/sd/job.nc"  # not consumed by the refused check


def test_a_failed_fetch_retries_once_the_backoff_elapses():
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    t.due_fetch(is_idle=True, now=0.0)
    t.note_fetch_failed("/sd/job.nc", now=0.0)
    assert t.due_fetch(is_idle=True, now=RETRY_BACKOFF_S) == "/sd/job.nc"


def test_repeated_failures_of_the_same_path_eventually_give_up():
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    now = 0.0
    for _ in range(MAX_FETCH_ATTEMPTS):
        path = t.due_fetch(is_idle=True, now=now)
        assert path == "/sd/job.nc"
        t.note_fetch_failed("/sd/job.nc", now=now)
        now += RETRY_BACKOFF_S
    # Exhausted: no longer queued, and a further idle tick asks for nothing.
    assert t.pending_path is None
    assert t.due_fetch(is_idle=True, now=now) is None


def test_a_fresh_publish_resets_the_attempt_count_after_giving_up():
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    now = 0.0
    for _ in range(MAX_FETCH_ATTEMPTS):
        t.due_fetch(is_idle=True, now=now)
        t.note_fetch_failed("/sd/job.nc", now=now)
        now += RETRY_BACKOFF_S
    assert t.pending_path is None

    # The machine announces it again (e.g. the job was restarted) -- this
    # is a fresh ask and gets a fresh attempt budget, not an immediate
    # second give-up.
    t.note_published_file("/sd/job.nc")
    assert t.due_fetch(is_idle=True, now=now) == "/sd/job.nc"


def test_a_fetch_failure_does_not_mark_the_path_loaded():
    """note_fetch_failed must never let a later note_published_file for the
    same path be dropped as already-loaded -- that was the actual bug: the
    old code called mark_loaded() even when the fetch failed."""
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    t.due_fetch(is_idle=True, now=0.0)
    t.note_fetch_failed("/sd/job.nc", now=0.0)
    t.note_published_file("/sd/job.nc")  # a later event re-announces it
    assert t.due_fetch(is_idle=True, now=0.0) == "/sd/job.nc"


# The player flag: the fourth value of the status report's P: field
# (P:<lines>,<percent>,<elapsed>,<playing>,...). The state word alone is not
# enough: the firmware reports Idle whenever the motion queue is empty,
# including mid-job during a tool change, a probe or the start-of-job
# routine, while the player is still playing.


def test_an_idle_report_while_the_player_is_playing_does_not_release_the_fetch():
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    for _ in range(passive_fetch.SETTLE_SAMPLES + 2):
        assert t.due_fetch(is_idle=True, playing=True) is None
    assert t.pending_path == "/sd/job.nc"  # still owed, not dropped


def test_a_job_with_idle_moments_is_fetched_only_after_the_player_stops():
    """The status sequence of a job that empties its motion queue mid-job
    (tool change, probe, start-of-job routine): Run and Idle alternate while
    the player flag stays 1. The fetch is released only once the player
    reports 0 at the end of the job."""
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    mid_job = [(False, True), (True, True), (True, True), (False, True), (True, True), (False, True)]
    for is_idle, playing in mid_job:
        assert t.due_fetch(is_idle=is_idle, playing=playing) is None
    results = [t.due_fetch(is_idle=True, playing=False) for _ in range(passive_fetch.SETTLE_SAMPLES)]
    assert results[-1] == "/sd/job.nc"
    assert all(r is None for r in results[:-1])


def test_one_not_playing_sample_after_a_publish_is_not_enough():
    """The report a controller holds when the event arrives may predate the
    event, so a release needs SETTLE_SAMPLES consecutive observations."""
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    for _ in range(passive_fetch.SETTLE_SAMPLES - 1):
        assert t.due_fetch(is_idle=True, playing=False) is None
    assert t.due_fetch(is_idle=True, playing=False) == "/sd/job.nc"


def test_a_playing_sample_restarts_the_settle_count():
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    for _ in range(passive_fetch.SETTLE_SAMPLES - 1):
        assert t.due_fetch(is_idle=True, playing=False) is None
    assert t.due_fetch(is_idle=True, playing=True) is None
    for _ in range(passive_fetch.SETTLE_SAMPLES - 1):
        assert t.due_fetch(is_idle=True, playing=False) is None
    assert t.due_fetch(is_idle=True, playing=False) == "/sd/job.nc"


def test_a_publish_restarts_the_settle_count():
    """Reports seen before the event do not count towards releasing it."""
    t = PassiveFetchTracker()
    for _ in range(passive_fetch.SETTLE_SAMPLES + 1):
        t.due_fetch(is_idle=True, playing=False)
    t.note_published_file("/sd/job.nc")
    assert t.due_fetch(is_idle=True, playing=False) is None
    assert t.pending_path == "/sd/job.nc"


def test_not_playing_but_not_idle_still_waits():
    """Jogging or an alarm with no job playing: the state word still has to
    say Idle, as without the flag."""
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    for _ in range(passive_fetch.SETTLE_SAMPLES + 2):
        assert t.due_fetch(is_idle=False, playing=False) is None
    assert t.pending_path == "/sd/job.nc"


def test_without_a_player_flag_a_single_idle_sample_releases_the_fetch():
    """Firmware that does not report the flag: the state word decides, on
    the first Idle report."""
    t = PassiveFetchTracker()
    t.note_published_file("/sd/job.nc")
    assert t.due_fetch(is_idle=False, playing=None) is None
    assert t.due_fetch(is_idle=True, playing=None) == "/sd/job.nc"


def test_player_flag_trusts_a_reported_playing_value():
    # Only firmware that reports the flag can set it, so 1 always means playing.
    assert passive_fetch.player_flag(1, reports_flag=True) is True
    assert passive_fetch.player_flag(1, reports_flag=False) is True


def test_player_flag_trusts_zero_only_from_firmware_that_reports_the_flag():
    assert passive_fetch.player_flag(0, reports_flag=True) is False
    # Zero is also the value when the field is absent, so it says nothing
    # about firmware that may not report it.
    assert passive_fetch.player_flag(0, reports_flag=False) is None
