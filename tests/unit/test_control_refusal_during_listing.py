"""A refused `ls` (or rm/mv/mkdir) must surface the firmware's reason and end
the load at once, the same style as test_passive_state.py's AC3 (a real
Controller against a real socket, FakeMachine).

Without a load in progress, a control-gate refusal (ControlToken::gate(),
src/libs/ControlToken.cpp) reaches Controller._handle_protocol_message() as
an ordinary MessageKind.LINE and goes straight to parseLine(), which already
shows it on the console (test_passive_state.py's AC3). While a load/ls is in
flight (``self.loadNUM != 0``), the same LINE used to be queued into
``load_buffer`` as if it were directory data instead -- the refusal was
never shown, and the caller (main.py's SHORT_LOAD_TIMEOUT polling) only
found out a load had failed once it timed out, with no reason at all.
"""

from __future__ import annotations

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, LOAD_DIR, Controller
from carveracontroller.machine.identity import ControllerIdentity
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")


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


def test_refusal_during_a_listing_is_surfaced_and_ends_the_load(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    # Stand in for an ls already sent and in flight (main.py's _start_machine_ls).
    controller.loadNUM = LOAD_DIR
    controller.loadEOF = False
    controller.loadERR = False
    controller.load_refused_reason = None

    assert m.send_normal_info(b"error:Refused -- PC has control\r\n")

    assert m.wait_until(lambda: controller.loadERR is True, timeout=2.0)
    assert controller.load_refused_reason is not None
    assert "Refused -- PC has control" in controller.load_refused_reason

    # Never queued as if it were directory listing data.
    assert controller.load_buffer.qsize() == 0

    seen = _drain_log_messages(controller)
    assert any(kind == Controller.MSG_ERROR and "Refused -- PC has control" in text for kind, text in seen)


def test_refusal_during_a_listing_without_a_named_holder_is_surfaced(machine, controller):
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    controller.loadNUM = LOAD_DIR
    controller.loadEOF = False
    controller.loadERR = False
    controller.load_refused_reason = None

    assert m.send_normal_info(b"error:Transfer refused -- an interactive move is in progress\r\n")

    assert m.wait_until(lambda: controller.loadERR is True, timeout=2.0)
    assert controller.load_buffer.qsize() == 0

    seen = _drain_log_messages(controller)
    assert any(
        kind == Controller.MSG_ERROR and "Transfer refused -- an interactive move is in progress" in text
        for kind, text in seen
    )


def test_ordinary_listing_data_is_not_mistaken_for_a_refusal(machine, controller):
    """Plain directory data must still reach load_buffer as before -- only
    the control gate's own refusal text is special-cased."""
    m = machine(mode="new")
    controller.open(CONN_WIFI, m.address())
    assert _wait_identified(m, controller)

    controller.loadNUM = LOAD_DIR
    controller.loadEOF = False
    controller.loadERR = False
    controller.load_refused_reason = None

    assert m.send_normal_info(b"part.nc 1024 123456\r\n")

    assert m.wait_until(lambda: controller.load_buffer.qsize() > 0, timeout=2.0)
    assert controller.loadERR is False
    assert controller.load_refused_reason is None
