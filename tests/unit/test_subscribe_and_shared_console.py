"""End-to-end tests for subscribing to published status, the automatic
heartbeat, and the shared console — against a real Controller and a real
socket (FakeMachine), the same style as test_controller_hello.py.

Covers the four functional acceptance criteria of "Controller: subscribe
instead of poll, heartbeat, shared console":

  1. Once identified, status comes from published ticks, not polling; a
     subscribed controller stops sending `?`.
  2. A heartbeat goes out, unprompted, within the timeout whenever nothing
     else was sent.
  3. Another controller's published lines show up in the console tagged
     with their source name, and never perturb this controller's own
     command/reply state (parseLine) — proven structurally, not just by
     absence of a symptom.
  4. Old firmware (publishes nothing, never acks hello) is completely
     unaffected: polling continues exactly as today, and no heartbeat is
     ever sent, since there is no subscription to keep alive.
"""

from __future__ import annotations

import time

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, STREAM_POLL, Controller
from carveracontroller.machine.heartbeat import HEARTBEAT_INTERVAL_S
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import PTYPE_CTRL_SINGLE, PTYPE_HEARTBEAT
from carveracontroller.protocols.messages import MessageKind, ParsedMessage
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")
OTHER_ID = 0x0807060504030201  # some other controller's id, never ours

# parseBracketAngle expects pipe-separated top-level fields (state|MPos:...|
# WPos:...), unlike fake_machine.py's own DEFAULT_STATUS (comma-only, used
# only where the exact parsed state has never mattered) — see
# Controller.parseBracketAngle's own docstring example. Used here so the
# parsed CNC state is an unambiguous "Idle", not the whole raw string.
PARSEABLE_STATUS = b"<Idle|MPos:1.000,2.000,3.000|WPos:1.000,2.000,3.000>"


@pytest.fixture
def machine():
    instances = []

    def _make(**kwargs):
        m = FakeMachine(**kwargs)
        instances.append(m)
        return m

    yield _make
    for m in instances:
        m.stop()


@pytest.fixture
def controller():
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    yield c
    c.close(allow_reconnect=False)


def _drain_log_messages(controller):
    """All (kind, text) pairs currently queued on controller.log, in order."""
    messages = []
    while True:
        try:
            messages.append(controller.log.get_nowait())
        except Exception:
            break
    return messages


def _wait_identified(machine_, controller_, timeout=2.0):
    return machine_.wait_until(
        lambda: controller_._hello is not None and controller_._hello.identified, timeout=timeout
    )


# -- AC1: subscribe, take status from publishes, stop polling ---------------


def test_subscribed_controller_stops_polling_once_identified(machine, controller):
    m = machine(mode="new", publish_status=True)
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    polls_at_subscribe = len(m.frames_of_type(PTYPE_CTRL_SINGLE))
    time.sleep(STREAM_POLL * 5)
    assert len(m.frames_of_type(PTYPE_CTRL_SINGLE)) == polls_at_subscribe, (
        "an identified (subscribed) controller must not keep sending `?` polls"
    )


def test_subscribed_controller_takes_status_from_an_unsolicited_publish(machine, controller):
    """The direct proof for AC1's second half: an unsolicited, unpolled
    status frame still updates machine state. CNC.vars["state"] is set to a
    sentinel right after subscribing (before the publish), so a change to
    "Idle" can only have come from the publish_status() call below, not
    from some earlier/residual value."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    CNC.vars["state"] = "unset-before-publish"
    assert m.publish_status(PARSEABLE_STATUS)
    assert m.wait_until(lambda: CNC.vars.get("state") == "Idle", timeout=1.0), (
        "an unsolicited published status tick must still update machine state"
    )


# -- AC2: heartbeat -----------------------------------------------------


def test_subscribed_idle_controller_sends_a_heartbeat_within_the_interval(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.wait_until(lambda: m.frames_of_type(PTYPE_HEARTBEAT) != [], timeout=HEARTBEAT_INTERVAL_S + 2.0)
    (payload,) = m.frames_of_type(PTYPE_HEARTBEAT)[:1]
    assert payload == b""


def test_heartbeat_is_not_resent_while_other_traffic_is_going_out(machine, controller):
    """A heartbeat is automatic traffic sent only when nothing else was —
    not a fixed-schedule tick. Keeping the link busy with ordinary sends
    must not also produce a steady stream of heartbeats stacked on top."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    deadline = time.time() + HEARTBEAT_INTERVAL_S * 3
    while time.time() < deadline:
        controller.executeCommand("version")
        time.sleep(0.1)

    # Some traffic went out, but nowhere near one heartbeat per 0.1s tick.
    assert len(m.frames_of_type(PTYPE_HEARTBEAT)) <= 1


# -- AC3: shared console -------------------------------------------------


def test_published_line_from_another_controller_is_shown_with_source_name(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.send_published_line(OTHER_ID, b"Shop PC", b"version")

    seen = []

    def _check():
        seen.extend(_drain_log_messages(controller))
        return any(kind == controller.MSG_PUBLISHED and text == "[Shop PC] version" for kind, text in seen)

    assert m.wait_until(_check, timeout=1.0)


def test_own_published_echo_is_not_shown_a_second_time(machine, controller):
    """The firmware publishes a client's own command/reply back to itself
    too (no sender exclusion) — the controller must filter its own
    source_id back out locally rather than show a duplicate of something
    it already showed (or sent) itself."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    assert m.send_published_line(IDENTITY.id, b"Test PC", b"version")
    time.sleep(0.3)

    seen = _drain_log_messages(controller)
    assert not any(kind == controller.MSG_PUBLISHED for kind, _text in seen)


def test_published_line_from_another_controller_never_reaches_own_reply_handling(machine, controller):
    """Proves AC3's isolation requirement structurally, not just by
    absence of a symptom: a foreign published line carrying the exact
    text of an alarm reply must not set CNC.vars["alarm_message"], while
    the identical text arriving as this controller's own reply (a LINE
    message, the same path a real reply takes) does. That contrast shows
    the isolation comes from PUBLISHED_LINE never being handed to
    parseLine, not from the text itself being somehow immune."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    CNC.vars["alarm_message"] = ""
    assert m.send_published_line(OTHER_ID, b"Shop PC", b"ERROR: someone else's fault")
    time.sleep(0.3)
    assert CNC.vars["alarm_message"] == "", "a published line from another controller must never reach parseLine"

    controller._handle_protocol_message(ParsedMessage(MessageKind.LINE, text="ERROR: own fault"))
    assert CNC.vars["alarm_message"] == "own fault", (
        "the controller's own reply handling must still work: this is what the isolation above is protecting, "
        "not a coincidence of the foreign text being ignored some other way"
    )


# -- AC4: old firmware, unaffected ---------------------------------------


def test_old_firmware_keeps_polling_and_never_subscribes(machine, controller):
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: len(m.hellos_received) >= 1, timeout=1.0)
    time.sleep(STREAM_POLL * 5)

    polls = m.frames_of_type(PTYPE_CTRL_SINGLE)
    assert len(polls) >= 2, "old firmware must still be polled exactly as today"
    assert all(p == b"?" for p in polls)
    assert controller._hello is not None and not controller._hello.identified


def test_old_firmware_never_receives_a_heartbeat(machine, controller):
    """The hard ADR-0001 compatibility constraint: old firmware, which
    publishes nothing and never acks hello, must see no new traffic shape
    at all, including no heartbeat — there is no subscription for a
    heartbeat to keep alive against it."""
    m = machine(mode="old")
    controller.open(CONN_WIFI, m.address())

    assert m.wait_until(lambda: len(m.hellos_received) >= 1, timeout=1.0)
    time.sleep(HEARTBEAT_INTERVAL_S + 1.0)

    assert m.frames_of_type(PTYPE_HEARTBEAT) == []
