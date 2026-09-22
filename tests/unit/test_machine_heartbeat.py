from carveracontroller.machine.heartbeat import HEARTBEAT_INTERVAL_S, heartbeat_due


def test_due_when_nothing_ever_sent():
    assert heartbeat_due(now=100.0, last_sent_at=None) is True


def test_not_due_just_after_something_was_sent():
    assert heartbeat_due(now=10.0, last_sent_at=10.0) is False
    assert heartbeat_due(now=10.0 + HEARTBEAT_INTERVAL_S - 0.01, last_sent_at=10.0) is False


def test_due_once_the_interval_has_fully_elapsed():
    assert heartbeat_due(now=10.0 + HEARTBEAT_INTERVAL_S, last_sent_at=10.0) is True
    assert heartbeat_due(now=10.0 + HEARTBEAT_INTERVAL_S + 5.0, last_sent_at=10.0) is True


def test_custom_interval_overrides_the_default():
    assert heartbeat_due(now=1.0, last_sent_at=0.0, interval_s=0.5) is True
    assert heartbeat_due(now=1.0, last_sent_at=0.6, interval_s=0.5) is False
