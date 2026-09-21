from carveracontroller.machine.hello import ACK_TIMEOUT_S, HELLO_WINDOW_S, OPEN_TIMEOUT_S, HelloNegotiator, Resolution
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
    # Still well within the open-wait deadline: nothing to time out yet.
    assert negotiator.poll(now=1.0) is False
    assert not negotiator.resolved


def test_fallback_after_open_timeout_elapses_with_no_valid_frame_ever():
    """Direct coverage for the state machine's half of the fix: a link that
    never produces a single CRC-valid frame. The ack-wait deadline (anchored
    on the first hello, itself only sent after a valid frame) never starts,
    so only the open-wait deadline (anchored at connection open,
    ``opened_at``) can resolve this. This exercises the new API
    (``opened_at``/``open_timeout_s`` didn't exist before this fix, so this
    exact test wouldn't run against the pre-fix code at all — it would fail
    at construction with a TypeError, not reach these assertions); the
    behavioural regression test that fails on the actual bug (poll() never
    firing, queue never flushing) is
    test_controller_hello.py::test_ordinary_command_flushed_after_open_timeout_against_a_silent_machine."""
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI, opened_at=0.0)

    assert negotiator.poll(now=OPEN_TIMEOUT_S - 0.01) is False
    assert not negotiator.resolved
    assert negotiator.frame_seen is False

    assert negotiator.poll(now=OPEN_TIMEOUT_S) is True
    assert negotiator.resolution is Resolution.FALLBACK
    # Fires exactly once.
    assert negotiator.poll(now=OPEN_TIMEOUT_S + 1) is False


def test_open_timeout_s_override_replaces_the_default():
    """Controller.open() passes an explicit, larger ``open_timeout_s`` for a
    USB-serial connect (which resets the machine right before this
    negotiator is built, so it may still be booting) instead of the
    OPEN_TIMEOUT_S default sized for an already-live WiFi/bulk-USB link. An
    override must actually replace the default, not just add to it."""
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI, opened_at=0.0, open_timeout_s=20.0)

    # Long past the *default* OPEN_TIMEOUT_S (3.0s): must not resolve yet.
    assert negotiator.poll(now=OPEN_TIMEOUT_S + 1.0) is False
    assert not negotiator.resolved

    assert negotiator.poll(now=20.0 - 0.01) is False
    assert negotiator.poll(now=20.0) is True
    assert negotiator.resolution is Resolution.FALLBACK


def test_open_timeout_s_defaults_to_the_module_constant():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)

    assert negotiator.open_timeout_s == OPEN_TIMEOUT_S


def test_open_timeout_is_measured_from_opened_at_not_from_a_fixed_zero():
    """The open-wait deadline is anchored at this negotiator's own
    ``opened_at`` (connection open), not at an arbitrary global zero — a
    negotiator opened later in wall-clock/monotonic time must not resolve
    before its own OPEN_TIMEOUT_S has actually elapsed since then."""
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI, opened_at=100.0)

    assert negotiator.poll(now=100.0 + OPEN_TIMEOUT_S - 0.01) is False
    assert not negotiator.resolved

    assert negotiator.poll(now=100.0 + OPEN_TIMEOUT_S) is True
    assert negotiator.resolution is Resolution.FALLBACK


def test_late_valid_frame_after_open_timeout_still_sends_hello():
    """The open-wait fallback only stops the controller from waiting on
    queued sends; it must not stop hello being sent, or identify being
    possible, once a valid frame does eventually arrive (a machine that was
    just slow to boot, not one that's actually broken)."""
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI, opened_at=0.0)
    negotiator.poll(now=OPEN_TIMEOUT_S)
    assert negotiator.resolution is Resolution.FALLBACK

    frame = negotiator.on_valid_frame(now=OPEN_TIMEOUT_S + 0.5)

    assert frame is not None
    assert frame[4] == 0x60  # PTYPE_HELLO
    assert negotiator.frame_seen is True


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


def test_duplicate_accepted_ack_does_not_report_newly_identified():
    """A repeated hello ack on an already-identified link changes nothing —
    a caller that triggers a one-off action on "newly identified" (like
    requesting the client list) must not repeat it on every re-ack."""
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)

    first = negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_ACCEPTED, 0))
    second = negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_ACCEPTED, 0))

    assert first is True
    assert second is False
    assert negotiator.identified is True


def test_re_hello_continues_past_the_ack_timeout_within_the_hello_window():
    """Re-hello must keep going after the controller's own 1.0s fallback —
    that timer is the controller's own decision to stop *waiting*, not the
    machine's. The machine may still be listening for a hello until its own
    window passes."""
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)
    negotiator.poll(now=ACK_TIMEOUT_S)  # functional fallback already happened
    assert negotiator.resolution is Resolution.FALLBACK

    frame = negotiator.on_status_reply(now=2 * ACK_TIMEOUT_S)

    assert frame is not None
    assert frame[4] == 0x60


def test_re_hello_stops_once_the_hello_window_has_passed():
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)

    # Still within the window: fires.
    assert negotiator.on_status_reply(now=HELLO_WINDOW_S - 0.01) is not None
    # Past the window: the machine has given up on this link; stop.
    assert negotiator.on_status_reply(now=HELLO_WINDOW_S + 1.0) is None


def test_repeated_status_replies_do_not_delay_fallback():
    """A status reply arriving right at the ack-timeout boundary triggers a
    re-hello (on_status_reply) — that resend must not push back poll()'s
    fallback deadline, or a steady stream of status replies from old
    firmware would defer the 1.0s fallback indefinitely."""
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)

    resent = negotiator.on_status_reply(now=ACK_TIMEOUT_S)
    assert resent is not None  # sanity: the re-hello did fire

    assert negotiator.poll(now=ACK_TIMEOUT_S) is True
    assert negotiator.resolution is Resolution.FALLBACK


def test_late_ack_after_fallback_still_identifies():
    """A lost-then-retried ack can identify the controller even after the
    1.0s window already forced a functional fallback."""
    negotiator = HelloNegotiator(identity=IDENTITY, link=LINK_WIFI)
    negotiator.on_valid_frame(now=0.0)
    negotiator.poll(now=ACK_TIMEOUT_S)
    assert negotiator.resolution is Resolution.FALLBACK

    negotiator.on_hello_ack(HelloAck(HELLO_PROTOCOL_VERSION, HELLO_ACCEPTED, 0))

    assert negotiator.identified is True
    # The functional resolution (what already happened to held-back sends)
    # does not retroactively change.
    assert negotiator.resolution is Resolution.FALLBACK
