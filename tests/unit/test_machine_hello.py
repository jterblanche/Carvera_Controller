from carveracontroller.machine.hello import ACK_TIMEOUT_S, HelloNegotiator, Resolution
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.handshake import (
    HELLO_ACCEPTED,
    HELLO_REJECTED_CAP,
    HELLO_REJECTED_OLD_CONTROLLER,
    LINK_WIFI,
    HelloAck,
)
from carveracontroller.protocols.makera import HELLO_PROTOCOL_VERSION

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Office PC")


def test_not_applicable_link_resolves_to_fallback_immediately_and_never_sends():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI, applicable=False)

    assert negotiator.resolved
    assert negotiator.resolution is Resolution.FALLBACK
    assert negotiator.on_valid_frame(now=0.0) is None


def test_hello_not_sent_before_a_valid_frame():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)

    assert not negotiator.resolved
    assert negotiator.poll(now=10.0) is False  # nothing sent yet, nothing to time out


def test_hello_sent_once_after_first_valid_frame():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)

    frame = negotiator.on_valid_frame(now=0.0)
    assert frame is not None
    assert frame[:2] == b"\x86\x68"
    assert frame[4] == 0x60  # PTYPE_HELLO

    # A second "valid frame" event (e.g. the next status reply) must not
    # resend hello — it was already sent once.
    assert negotiator.on_valid_frame(now=0.1) is None


def test_fallback_after_ack_timeout_elapses():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)

    assert negotiator.poll(now=ACK_TIMEOUT_S - 0.01) is False
    assert not negotiator.resolved

    assert negotiator.poll(now=ACK_TIMEOUT_S) is True
    assert negotiator.resolution is Resolution.FALLBACK
    # Fires exactly once.
    assert negotiator.poll(now=ACK_TIMEOUT_S + 1) is False


def test_accepted_ack_before_timeout_resolves_identified():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)

    newly_identified = negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_ACCEPTED, 0))

    assert newly_identified is True
    assert negotiator.identified is True
    assert negotiator.resolution is Resolution.IDENTIFIED
    # The timeout must no longer fire once resolved.
    assert negotiator.poll(now=100.0) is False


def test_ack_result_cap_resolves_rejected():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)

    newly_identified = negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_REJECTED_CAP, 0))

    assert newly_identified is False
    assert negotiator.identified is False
    assert negotiator.resolution is Resolution.REJECTED


def test_ack_result_old_controller_present_resolves_rejected():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)

    negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_REJECTED_OLD_CONTROLLER, 0))

    assert negotiator.resolution is Resolution.REJECTED


def test_unrecognised_protocol_version_is_ignored():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)

    newly_identified = negotiator.on_hello_ack(HelloAck(protocol_version=99, result=HELLO_ACCEPTED, mode=0))

    assert newly_identified is False
    assert not negotiator.resolved


def test_re_hello_on_status_reply_when_still_unidentified_and_stale():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)

    # Too soon: no re-hello yet.
    assert negotiator.on_status_reply(now=0.5) is None

    # Stale (a full ack window has passed with no ack): re-hello.
    frame = negotiator.on_status_reply(now=ACK_TIMEOUT_S)
    assert frame is not None
    assert frame[4] == 0x60


def test_re_hello_stops_once_identified():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)
    negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_ACCEPTED, 0))

    assert negotiator.on_status_reply(now=100.0) is None


def test_late_ack_after_fallback_still_identifies():
    """A lost-then-retried ack can identify the controller even after the
    1.0s window already forced a functional fallback (protocol doc §4.2)."""
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)
    negotiator.poll(now=ACK_TIMEOUT_S)
    assert negotiator.resolution is Resolution.FALLBACK

    negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_ACCEPTED, 0))

    assert negotiator.identified is True
    # The functional resolution (what already happened to held-back sends)
    # does not retroactively change.
    assert negotiator.resolution is Resolution.FALLBACK
