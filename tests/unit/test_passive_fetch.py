"""Pure decision tests for PassiveFetchTracker (machine/passive_fetch.py):
when a passive controller should fetch and draw a job file it did not
select itself, versus just showing progress until the machine goes idle."""

from __future__ import annotations

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
