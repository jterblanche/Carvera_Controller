"""The hello's launch part, and hello-ack results 3 and 4.

Result 3: another controller with this identity is connected and answered
the machine's presence check. Result 4: the machine is checking another
hello and has not looked at this one; send the same hello again about a
second later, without telling the user, and give up after about 30 s.
"""

from __future__ import annotations

from carveracontroller.machine import hello as hello_module
from carveracontroller.machine.hello import (
    ACK_TIMEOUT_S,
    BUSY_GIVE_UP_S,
    BUSY_RETRY_S,
    REHELLO_INTERVAL_S,
    HelloNegotiator,
    Resolution,
)
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import validate_packet_data
from carveracontroller.protocols.handshake import (
    HELLO_ACCEPTED,
    HELLO_BUSY,
    HELLO_FEATURE_JOB_START_WAIT,
    HELLO_REJECTED_CAP,
    HELLO_REJECTED_IDENTITY_CONNECTED,
    LINK_WIFI,
    HelloAck,
)
from carveracontroller.protocols.makera import HELLO_PROTOCOL_VERSION, encode_hello

LAUNCH = 0x6512_3456_89AB_CDEF
IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Office PC", launch=LAUNCH)


def ack(result):
    return HelloAck(HELLO_PROTOCOL_VERSION, result, 0, 0)


def payload_of(frame):
    parsed = validate_packet_data(frame[2:-2])
    assert parsed is not None
    return parsed.payload


def started(identity=IDENTITY):
    negotiator = HelloNegotiator(identity=identity, link=LINK_WIFI)
    first = negotiator.on_valid_frame(now=0.0)
    assert first is not None
    return negotiator, first


# --- the launch part on the wire ------------------------------------------------


def test_hello_carries_the_launch_part_after_the_features_byte():
    _negotiator, frame = started()
    payload = payload_of(frame)

    name_len = payload[9]
    link_at = 10 + name_len
    assert payload[link_at] == LINK_WIFI
    assert payload[link_at + 1] == HELLO_FEATURE_JOB_START_WAIT
    assert payload[link_at + 2 :] == LAUNCH.to_bytes(8, "big")


def test_hello_keeps_every_older_field_where_it_was():
    """Firmware that reads up to the link or the features byte sees exactly
    the bytes it saw before; the launch part only adds bytes at the end."""
    _negotiator, frame = started()
    with_launch = payload_of(frame)
    before = payload_of(
        encode_hello(IDENTITY.id, IDENTITY.name.encode(), LINK_WIFI, features=HELLO_FEATURE_JOB_START_WAIT)
    )

    assert with_launch[: len(before)] == before
    assert len(with_launch) == len(before) + 8


def test_launch_part_without_features_still_sends_a_features_byte():
    payload = payload_of(encode_hello(7, b"A", LINK_WIFI, launch=LAUNCH))

    assert payload[-9] == 0
    assert payload[-8:] == LAUNCH.to_bytes(8, "big")


def test_identity_without_a_launch_part_sends_the_hello_as_before():
    _negotiator, frame = started(ControllerIdentity(id=1, name="A"))
    payload = payload_of(frame)

    assert payload[-1] == HELLO_FEATURE_JOB_START_WAIT
    assert len(payload) == 1 + 8 + 1 + 1 + 1 + 1


def test_every_hello_of_one_launch_carries_the_same_launch_part():
    negotiator, first = started()
    again = negotiator.on_status_reply(now=REHELLO_INTERVAL_S)

    assert again == first


# --- waiting for the ack --------------------------------------------------------


def test_a_held_hello_has_at_least_three_seconds_for_its_ack():
    """The machine can hold a hello for up to 2 s while it asks the other
    controller; the ack must not be given up on before then."""
    assert ACK_TIMEOUT_S >= 3.0
    negotiator, _ = started()

    assert negotiator.poll(now=2.5) is False
    negotiator.on_hello_ack(ack(HELLO_ACCEPTED), now=2.5)

    assert negotiator.resolution is Resolution.IDENTIFIED


def test_re_hello_on_a_live_link_still_comes_about_once_a_second():
    assert REHELLO_INTERVAL_S == 1.0
    negotiator, _ = started()

    assert negotiator.on_status_reply(now=0.9) is None
    assert negotiator.on_status_reply(now=1.0) is not None


# --- result 3: identity already connected ---------------------------------------


def test_result_3_refuses_and_says_why():
    negotiator, _ = started()

    newly = negotiator.on_hello_ack(ack(HELLO_REJECTED_IDENTITY_CONNECTED), now=2.0)

    assert newly is False
    assert negotiator.identified is False
    assert negotiator.resolution is Resolution.REJECTED
    assert negotiator.refusal == HELLO_REJECTED_IDENTITY_CONNECTED


def test_result_3_after_the_ack_wait_ran_out_still_refuses():
    """The controller has already sent its queued commands as for old
    firmware, but the machine has refused it: it must still close and say
    why, not stay connected unidentified."""
    negotiator, _ = started()
    assert negotiator.poll(now=ACK_TIMEOUT_S) is True

    negotiator.on_hello_ack(ack(HELLO_REJECTED_IDENTITY_CONNECTED), now=ACK_TIMEOUT_S + 0.5)

    assert negotiator.refusal == HELLO_REJECTED_IDENTITY_CONNECTED


def test_other_refusals_keep_their_meaning():
    negotiator, _ = started()

    negotiator.on_hello_ack(ack(HELLO_REJECTED_CAP), now=0.1)

    assert negotiator.resolution is Resolution.REJECTED
    assert negotiator.refusal == HELLO_REJECTED_CAP


def test_no_refusal_before_any_ack_or_after_acceptance():
    negotiator, _ = started()
    assert negotiator.refusal is None

    negotiator.on_hello_ack(ack(HELLO_ACCEPTED), now=0.1)

    assert negotiator.refusal is None


def test_result_3_never_retries():
    negotiator, _ = started()
    negotiator.on_hello_ack(ack(HELLO_REJECTED_IDENTITY_CONNECTED), now=0.5)

    assert negotiator.due_hello(now=5.0) is None
    assert negotiator.on_status_reply(now=1.5) is None


# --- result 4: busy, send the hello again shortly -------------------------------


def test_result_4_is_not_a_refusal():
    negotiator, _ = started()

    newly = negotiator.on_hello_ack(ack(HELLO_BUSY), now=0.1)

    assert newly is False
    assert negotiator.refusal is None
    assert not negotiator.resolved


def test_result_4_sends_the_same_hello_again_about_a_second_later():
    negotiator, first = started()
    negotiator.on_hello_ack(ack(HELLO_BUSY), now=0.1)

    assert negotiator.due_hello(now=0.1 + BUSY_RETRY_S - 0.01) is None
    again = negotiator.due_hello(now=0.1 + BUSY_RETRY_S)

    assert again == first
    # Once per busy answer.
    assert negotiator.due_hello(now=0.1 + BUSY_RETRY_S + 0.5) is None


def test_result_4_keeps_retrying_while_the_answer_is_4_then_identifies():
    negotiator, first = started()
    now = 0.1
    sent = []
    for _ in range(5):
        negotiator.on_hello_ack(ack(HELLO_BUSY), now=now)
        now += BUSY_RETRY_S
        frame = negotiator.due_hello(now=now)
        assert negotiator.poll(now=now) is False
        sent.append(frame)
        now += 0.05

    assert sent == [first] * 5
    assert negotiator.on_hello_ack(ack(HELLO_ACCEPTED), now=now) is True
    assert negotiator.resolution is Resolution.IDENTIFIED
    assert negotiator.due_hello(now=now + 10) is None


def test_waiting_to_retry_never_falls_back_to_old_firmware_behaviour():
    negotiator, _ = started()
    negotiator.on_hello_ack(ack(HELLO_BUSY), now=2.9)

    # The first hello's ack wait would have run out at 3.0 s.
    assert negotiator.poll(now=3.5) is False
    assert negotiator.due_hello(now=2.9 + BUSY_RETRY_S) is not None
    # The ack wait now runs from the retried hello.
    assert negotiator.poll(now=2.9 + BUSY_RETRY_S + ACK_TIMEOUT_S - 0.01) is False
    assert negotiator.poll(now=2.9 + BUSY_RETRY_S + ACK_TIMEOUT_S) is True


def test_a_live_status_reply_does_not_add_hellos_while_a_retry_is_pending():
    negotiator, _ = started()
    negotiator.on_hello_ack(ack(HELLO_BUSY), now=0.5)

    assert negotiator.on_status_reply(now=1.2) is None


def test_re_hello_window_restarts_with_each_busy_answer():
    """The machine restarts this connection's 5 s hello window with every
    result 4, so the controller's own re-hello window follows it."""
    negotiator, first = started()
    now = 0.1
    while now < 8.0:
        negotiator.on_hello_ack(ack(HELLO_BUSY), now=now)
        now += BUSY_RETRY_S
        negotiator.due_hello(now=now)
        now += 0.05

    # A hello lost on the wire after the original 5 s window is still re-sent.
    assert negotiator.on_status_reply(now=now + REHELLO_INTERVAL_S) == first


def test_result_4_gives_up_after_about_30_seconds_with_machine_busy():
    negotiator, _ = started()
    now = 0.1
    while negotiator.refusal is None and now < 100:
        negotiator.on_hello_ack(ack(HELLO_BUSY), now=now)
        now += BUSY_RETRY_S
        negotiator.due_hello(now=now)
        now += 0.05

    assert negotiator.refusal == HELLO_BUSY
    assert negotiator.resolution is Resolution.REJECTED
    assert BUSY_GIVE_UP_S - 2 <= now <= BUSY_GIVE_UP_S + 3
    assert negotiator.due_hello(now=now + 5) is None


def test_busy_timing_constants_follow_the_firmware():
    assert BUSY_RETRY_S == 1.0
    assert BUSY_GIVE_UP_S == 30.0


def test_busy_constants_are_read_when_used(monkeypatch):
    monkeypatch.setattr(hello_module, "BUSY_RETRY_S", 0.2)
    negotiator, first = started()
    negotiator.on_hello_ack(ack(HELLO_BUSY), now=0.0)

    assert negotiator.due_hello(now=0.2) == first


def test_a_busy_answer_after_identification_is_ignored():
    negotiator, _ = started()
    negotiator.on_hello_ack(ack(HELLO_ACCEPTED), now=0.1)

    negotiator.on_hello_ack(ack(HELLO_BUSY), now=0.2)

    assert negotiator.identified
    assert negotiator.due_hello(now=5.0) is None
