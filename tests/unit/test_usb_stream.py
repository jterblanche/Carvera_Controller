"""Tests for USBStream's serial failure semantics.

The USB counterpart of the distinction WIFIStream.recv() already makes for
a TCP socket: an ordinary empty read (nothing available yet) must not be
mistaken for the device itself being gone. A TCP socket tells the two apart
by its return value (b"" only on a genuine close, socket.timeout otherwise);
a serial port has no such return-value signal — an empty read is routine —
so a failure shows up as the call itself raising.

Two kinds of failure are told apart:

- The device went away (cable unplugged, machine powered off). pyserial
  reports this differently per platform and per call, and USBStream
  recognises those forms (USBStream.is_device_gone). The port is torn down quietly,
  with the exception kept in the debug log only, and the controller's
  ordinary connection-lost handling reports the disconnect.
- Anything else. A failed read or write raises PeerClosedError and tears
  the port down; a failed in_waiting poll is raised unchanged.
"""

import errno
import threading
import time
from unittest.mock import patch

import pytest
import serial

import carveracontroller.USBStream as usb_stream_module
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_USB, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.machine.peer_closed import PeerClosedError
from carveracontroller.USBStream import USBStream

# The exact text pyserial's Windows backend raised on every call once the
# cable was pulled: ClearCommError() on the still-open handle fails with
# ERROR_ACCESS_DENIED (winerror 5).
WINDOWS_UNPLUG = "ClearCommError failed (PermissionError(13, 'Access is denied.', None, 5))"

DEVICE_GONE = [
    # Windows (serialwin32): "<call> failed (<repr of ctypes.WinError()>)".
    # The winerror is the last number; the text in between is localised.
    serial.SerialException(WINDOWS_UNPLUG),
    serial.SerialException("ReadFile failed (PermissionError(13, 'Access is denied.', None, 5))"),
    serial.SerialException("WriteFile failed (PermissionError(13, 'Access is denied.', None, 5))"),
    serial.SerialException("ClearCommError failed (PermissionError(13, 'Zugriff verweigert', None, 5))"),
    serial.SerialException(
        "ClearCommError failed (OSError(22, 'The device does not recognize the command.', None, 22))"
    ),
    serial.SerialException(
        "WriteFile failed (OSError(22, 'A device attached to the system is not functioning.', None, 31))"
    ),
    serial.SerialException("GetOverlappedResult failed (OSError(22, 'The device is not connected.', None, 1167))"),
    # Linux and macOS (serialposix): in_waiting is a TIOCINQ query that
    # raises OSError directly; read() and write() wrap the OSError.
    OSError(errno.EIO, "Input/output error"),
    OSError(errno.ENXIO, "Device not configured"),
    OSError(errno.ENODEV, "No such device"),
    serial.SerialException("read failed: [Errno 5] Input/output error"),
    serial.SerialException("write failed: [Errno 6] Device not configured"),
    serial.SerialException(
        "device reports readiness to read but returned no data (device disconnected or multiple access on port?)"
    ),
]

NOT_DEVICE_GONE = [
    # Opening a port another program already holds: the same Windows
    # "Access is denied", but at open time, which is a real error to show.
    serial.SerialException("could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"),
    serial.SerialException("could not open port /dev/ttyACM0: [Errno 16] Device or resource busy: '/dev/ttyACM0'"),
    serial.SerialTimeoutException("Write timeout"),
    serial.SerialException("ClearCommError failed (OSError(22, 'The parameter is incorrect.', None, 87))"),
    serial.SerialException("read failed: [Errno 22] Invalid argument"),
    serial.SerialException("Attempting to use a port that is not open"),
    OSError(errno.EBADF, "Bad file descriptor"),
    ValueError("not a serial failure"),
]


@pytest.mark.parametrize("exc", DEVICE_GONE, ids=str)
def test_device_gone_forms_are_recognised(exc):
    assert usb_stream_module.is_device_gone(exc) is True


@pytest.mark.parametrize("exc", NOT_DEVICE_GONE, ids=str)
def test_other_failures_are_not_mistaken_for_the_device_going(exc):
    assert usb_stream_module.is_device_gone(exc) is False


class _FailingSerial:
    """A serial.Serial double whose read() or write() raises the given
    exception."""

    def __init__(self, fail_on, exc=None):
        self.fail_on = fail_on  # "read" or "write"
        self.exc = exc or serial.SerialException(
            "ReadFile failed (OSError(22, 'The parameter is incorrect.', None, 87))"
        )
        self.closed = False

    def read(self, size=1):
        if self.fail_on == "read":
            raise self.exc
        return b""

    def write(self, data):
        if self.fail_on == "write":
            raise self.exc
        return len(data)

    def close(self):
        self.closed = True


class _UnpluggedSerial:
    """A serial.Serial double for a port whose device has gone: every call
    on it raises, as pyserial does once the cable is out."""

    def __init__(self, exc):
        self.exc = exc
        self.closed = False

    @property
    def in_waiting(self):
        raise self.exc

    def read(self, size=1):
        raise self.exc

    def write(self, data):
        raise self.exc

    def close(self):
        self.closed = True


class _OrdinaryTimeoutSerial:
    """A serial.Serial double for a live, working port with nothing to
    read right now — the routine case an empty read must not be confused
    with a failure."""

    def read(self, size=1):
        return b""

    def write(self, data):
        return len(data)


def _stream_with(fake_serial):
    stream = USBStream.__new__(USBStream)
    stream.serial = fake_serial
    stream.log_sent_receive = False
    stream._send_log_buffer = b""
    stream._recv_log_buffer = b""
    return stream


def test_recv_raises_peer_closed_error_and_tears_down_the_port_on_serial_exception():
    fake = _FailingSerial(fail_on="read")
    stream = _stream_with(fake)

    with pytest.raises(PeerClosedError):
        stream.recv()

    assert stream.serial is None
    assert fake.closed is True


def test_send_raises_peer_closed_error_and_tears_down_the_port_on_serial_exception():
    fake = _FailingSerial(fail_on="write")
    stream = _stream_with(fake)

    with pytest.raises(PeerClosedError):
        stream.send(b"?")

    assert stream.serial is None
    assert fake.closed is True


def test_recv_of_empty_bytes_on_an_ordinary_timeout_is_not_a_peer_close():
    """Regression guard for the crux of this fix: an empty read with
    nothing wrong must never be treated as the device having gone, unlike
    a TCP socket's b"" (see the CONN_WIFI-only gate in
    Controller.streamIO)."""
    stream = _stream_with(_OrdinaryTimeoutSerial())

    assert stream.recv() == b""
    assert stream.serial is not None


def test_send_on_an_ordinary_working_port_does_not_raise():
    stream = _stream_with(_OrdinaryTimeoutSerial())

    stream.send(b"?")  # must not raise

    assert stream.serial is not None


def _call(stream, op):
    if op == "waiting_for_recv":
        return stream.waiting_for_recv()
    if op == "recv":
        return stream.recv()
    return stream.send(b"?")


QUIET_RESULT = {"waiting_for_recv": 0, "recv": b"", "send": None}

# A bare OSError only ever comes from the in_waiting TIOCINQ query: pyserial's
# read() and write() wrap theirs in SerialException.
QUIET_CASES = [
    (op, exc)
    for exc in DEVICE_GONE
    for op in sorted(QUIET_RESULT)
    if isinstance(exc, serial.SerialException) or op == "waiting_for_recv"
]


@pytest.mark.parametrize(("op", "exc"), QUIET_CASES, ids=lambda value: str(value))
def test_device_gone_tears_the_port_down_quietly(op, exc):
    """Whichever call notices first, a device that went away closes the
    port and reports nothing to read or send, without raising. The
    exception text goes to the debug log, never to an error-level log."""
    fake = _UnpluggedSerial(exc)
    stream = _stream_with(fake)

    with patch.object(usb_stream_module, "logger") as log:
        assert _call(stream, op) == QUIET_RESULT[op]

    assert stream.serial is None
    assert fake.closed is True
    log.error.assert_not_called()
    log.exception.assert_not_called()
    log.warning.assert_not_called()
    debug_lines = [call.args[0] % call.args[1:] for call in log.debug.call_args_list]
    assert any(str(exc) in line for line in debug_lines)


def test_other_failure_while_polling_for_input_is_raised_unchanged():
    exc = serial.SerialException("ClearCommError failed (OSError(22, 'The parameter is incorrect.', None, 87))")
    fake = _UnpluggedSerial(exc)
    stream = _stream_with(fake)

    with pytest.raises(serial.SerialException) as raised:
        stream.waiting_for_recv()

    assert raised.value is exc
    assert stream.serial is fake
    assert fake.closed is False


def test_open_still_raises_when_another_program_holds_the_port():
    exc = serial.SerialException("could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)")
    stream = USBStream()

    with (
        patch.object(usb_stream_module.serial, "serial_for_url", side_effect=exc),
        pytest.raises(serial.SerialException) as raised,
    ):
        stream.open("COM3")

    assert raised.value is exc


def test_unplug_does_not_put_the_exception_on_the_console_and_leaves_the_link_to_the_lost_check():
    """The reported case, through the real Controller.streamIO loop: a USB
    link whose cable is pulled, so in_waiting raises Windows' ClearCommError
    on every poll. Nothing may reach the console log (Controller.log, shown
    in the app; MSG_ERROR is the red line). The stream stays attached and
    the disconnect is not marked as deliberate, so the app's ordinary
    connection-lost check (no status within its heartbeat timeout) reports
    it and offers to reconnect, as for any other lost link."""
    controller = Controller(CNC(), callback=None, identity=ControllerIdentity(id=0x0102030405060708, name="Test PC"))
    fake = _UnpluggedSerial(serial.SerialException(WINDOWS_UNPLUG))
    stream = _stream_with(fake)
    controller.connection_type = CONN_USB
    controller.stream = stream
    controller.stop.clear()
    controller.thread = threading.Thread(target=controller.streamIO)
    controller.thread.start()
    try:
        time.sleep(0.3)
        assert controller.thread.is_alive()
    finally:
        controller.stop.set()
        controller.thread.join(timeout=1.0)
        controller.stop.clear()

    try:
        messages = []
        while not controller.log.empty():
            messages.append(controller.log.get_nowait())
        assert [text for kind, text in messages if "ClearCommError" in str(text)] == []
        assert [text for kind, text in messages if kind == Controller.MSG_ERROR] == []
        assert controller.stream is stream
        assert controller._manual_disconnect is False
        assert stream.serial is None
        assert fake.closed is True
    finally:
        controller.close(allow_reconnect=False)
