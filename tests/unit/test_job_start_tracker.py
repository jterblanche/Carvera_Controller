"""Pure tests for JobStartTracker (machine/job_start.py): what a waiting
job-start event asks of this controller, and the countdown."""

from __future__ import annotations

from carveracontroller.machine.job_start import (
    MIN_SECONDS_LEFT_TO_RETRY,
    RETRY_BACKOFF_S,
    STALE_AFTER_LIMIT_S,
    JobStartAction,
    JobStartTracker,
)
from carveracontroller.protocols.handshake import (
    JOB_START_CANCELLED,
    JOB_START_HASHING,
    JOB_START_REASON_ABORT,
    JOB_START_REASON_ALL_READY,
    JOB_START_REASON_START_NOW,
    JOB_START_REASON_WAITING,
    JOB_START_STARTING,
    JOB_START_WAITING,
    JobStartEvent,
)

ME = 0x2222
STARTER = 0x1111
OTHER = 0x3333


def _event(start_id=7, phase=JOB_START_WAITING, reason=JOB_START_REASON_WAITING, seconds_left=30, **kw):
    fields = {
        "path": "/sd/gcodes/part.nc",
        "size": 6,
        "checksum": b"\x01" * 16,
        "start_id": start_id,
        "phase": phase,
        "reason": reason,
        "seconds_left": seconds_left,
        "starter_id": STARTER,
        "not_ready_ids": (ME,),
    }
    fields.update(kw)
    return JobStartEvent(**fields)


def test_first_waiting_event_listing_me_asks_to_prepare():
    t = JobStartTracker(own_id=ME)
    assert t.on_event(_event(), now=0.0) is JobStartAction.PREPARE
    assert t.held
    assert not t.is_starter


def test_nothing_more_is_asked_while_preparing():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(), now=0.0)
    t.begin_preparing(7)
    assert t.on_event(_event(seconds_left=29), now=1.0) is JobStartAction.NONE


def test_ready_is_sent_once_prepared_and_resent_while_still_listed():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(), now=0.0)
    t.begin_preparing(7)
    assert t.prepared(7) is True
    # The ready was lost: the machine still lists this controller.
    assert t.on_event(_event(seconds_left=28), now=2.0) is JobStartAction.RESEND_READY
    assert t.on_event(_event(seconds_left=27), now=3.0) is JobStartAction.RESEND_READY
    # Once the machine has it, nothing more.
    assert t.on_event(_event(seconds_left=26, not_ready_ids=(OTHER,)), now=4.0) is JobStartAction.NONE


def test_an_event_not_listing_me_asks_nothing():
    t = JobStartTracker(own_id=ME)
    assert t.on_event(_event(not_ready_ids=(OTHER,)), now=0.0) is JobStartAction.NONE
    assert t.held  # the countdown is still shown


def test_the_starter_is_never_asked_to_prepare():
    t = JobStartTracker(own_id=STARTER)
    assert t.on_event(_event(not_ready_ids=(ME, STARTER)), now=0.0) is JobStartAction.NONE
    assert t.is_starter


def test_a_new_start_id_starts_over():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(start_id=7), now=0.0)
    t.begin_preparing(7)
    t.prepared(7)
    assert t.on_event(_event(start_id=8), now=5.0) is JobStartAction.PREPARE


def test_ready_for_a_start_no_longer_held_is_not_sent():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(start_id=7), now=0.0)
    t.begin_preparing(7)
    t.on_event(_event(phase=JOB_START_STARTING, reason=JOB_START_REASON_ALL_READY, seconds_left=0), now=1.0)
    assert t.prepared(7) is False


def test_starting_and_cancelled_end_the_hold():
    for phase, reason in (
        (JOB_START_STARTING, JOB_START_REASON_ALL_READY),
        (JOB_START_CANCELLED, JOB_START_REASON_ABORT),
    ):
        t = JobStartTracker(own_id=ME)
        t.on_event(_event(), now=0.0)
        assert t.on_event(_event(phase=phase, reason=reason, seconds_left=0), now=1.0) is JobStartAction.NONE
        assert not t.held
        assert t.event is None


def test_countdown_runs_on_between_events():
    """No event is sent while a file transfer runs, so the countdown counts
    down locally from the last event."""
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(seconds_left=30), now=100.0)
    assert t.seconds_left(100.0) == 30
    assert t.seconds_left(104.5) == 26
    assert t.seconds_left(140.0) == 0
    t.on_event(_event(seconds_left=20), now=110.0)
    assert t.seconds_left(110.0) == 20


def test_a_hold_whose_end_was_lost_goes_stale():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(seconds_left=30), now=0.0)
    assert not t.stale(30.0 + STALE_AFTER_LIMIT_S)
    assert t.stale(30.0 + STALE_AFTER_LIMIT_S + 0.1)
    assert not JobStartTracker(own_id=ME).stale(1000.0)


def test_a_failed_attempt_is_retried_after_a_backoff():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(), now=0.0)
    t.begin_preparing(7)
    t.failed(7, now=0.0)
    assert not t.ready_sent
    assert t.on_event(_event(seconds_left=29), now=RETRY_BACKOFF_S - 0.5) is JobStartAction.NONE
    assert t.on_event(_event(seconds_left=28), now=RETRY_BACKOFF_S) is JobStartAction.PREPARE


def test_the_backoff_doubles():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(), now=0.0)
    t.failed(7, now=0.0)
    t.on_event(_event(seconds_left=28), now=RETRY_BACKOFF_S)
    t.begin_preparing(7)
    t.failed(7, now=RETRY_BACKOFF_S)
    later = RETRY_BACKOFF_S + 2 * RETRY_BACKOFF_S
    assert t.on_event(_event(seconds_left=25), now=later - 0.5) is JobStartAction.NONE
    assert t.on_event(_event(seconds_left=24), now=later) is JobStartAction.PREPARE


def test_no_retry_with_too_little_time_left():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(), now=0.0)
    t.failed(7, now=0.0)
    left = MIN_SECONDS_LEFT_TO_RETRY - 1
    assert t.on_event(_event(seconds_left=left), now=100.0) is JobStartAction.NONE


def test_given_up_asks_for_nothing_more():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(), now=0.0)
    t.give_up(7)
    assert t.on_event(_event(seconds_left=20), now=1000.0) is JobStartAction.NONE
    assert not t.ready_sent


def test_ready_sent_is_known_until_the_hold_ends():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(), now=0.0)
    t.begin_preparing(7)
    t.prepared(7)
    assert t.ready_sent
    t.on_event(_event(phase=JOB_START_STARTING, reason=JOB_START_REASON_ALL_READY, seconds_left=0), now=1.0)
    assert not t.ready_sent


def test_a_new_start_forgets_earlier_failures():
    t = JobStartTracker(own_id=ME)
    t.on_event(_event(start_id=7), now=0.0)
    t.give_up(7)
    assert t.on_event(_event(start_id=8), now=1.0) is JobStartAction.PREPARE


# -- the machine checking the file first ---------------------------------------


def _hashing(**kw):
    return _event(phase=JOB_START_HASHING, seconds_left=0, checksum=b"", **kw)


def test_hashing_holds_the_start_but_asks_nothing():
    """While the machine computes the file's MD5 there is nothing to compare
    a copy with: nothing is drawn, fetched or sent, and there is no
    countdown yet."""
    t = JobStartTracker(own_id=ME)
    assert t.on_event(_hashing(), now=0.0) is JobStartAction.NONE
    assert t.held
    assert t.hashing
    assert t.seconds_left(0.0) == 0
    assert t.on_event(_hashing(), now=1.0) is JobStartAction.NONE


def test_the_first_waiting_event_after_hashing_starts_the_countdown_and_asks_to_prepare():
    t = JobStartTracker(own_id=ME)
    t.on_event(_hashing(), now=0.0)
    assert t.on_event(_event(seconds_left=30), now=12.0) is JobStartAction.PREPARE
    assert not t.hashing
    assert t.seconds_left(12.0) == 30
    t.begin_preparing(7)
    assert t.prepared(7) is True


def test_the_starter_is_shown_as_starter_while_hashing():
    t = JobStartTracker(own_id=STARTER)
    assert t.on_event(_hashing(not_ready_ids=(ME,)), now=0.0) is JobStartAction.NONE
    assert t.is_starter
    assert t.hashing


def test_starting_or_cancelled_straight_from_hashing_ends_the_hold():
    for phase, reason in (
        (JOB_START_STARTING, JOB_START_REASON_START_NOW),
        (JOB_START_CANCELLED, JOB_START_REASON_ABORT),
    ):
        t = JobStartTracker(own_id=ME)
        t.on_event(_hashing(), now=0.0)
        ended = _event(phase=phase, reason=reason, seconds_left=0, checksum=b"")
        assert t.on_event(ended, now=5.0) is JobStartAction.NONE
        assert not t.held
        assert not t.hashing


def test_a_hashing_hold_whose_events_stop_is_dropped():
    """Hashing events come once a second; a hold that hears nothing more
    for well past that is dropped as for a lost end."""
    t = JobStartTracker(own_id=ME)
    t.on_event(_hashing(), now=100.0)
    assert not t.stale(100.0 + STALE_AFTER_LIMIT_S)
    assert t.stale(100.0 + STALE_AFTER_LIMIT_S + 1)
