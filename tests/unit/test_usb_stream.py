"""Tests for USBStream's serial failure semantics.

The USB counterpart of the distinction WIFIStream.recv() already makes for
a TCP socket: an ordinary empty read (nothing available yet) must not be
mistaken for the device itself being gone. A TCP socket tells the two apart
by its return value (b"" only on a genuine close, socket.timeout otherwise);
a serial port has no such return-value signal — an empty read is routine —
so the device actually failing shows up as the read or write itself
raising serial.SerialException. These tests pin that USBStream converts
that into PeerClosedError (and tears the port down) without also raising it
for a routine empty read.
"""

import pytest
import serial

from carveracontroller.machine.peer_closed import PeerClosedError
from carveracontroller.USBStream import USBStream


class _FailingSerial:
    """A serial.Serial double whose read() or write() raises like a device
    that has just disappeared — unplugged, or the OS reclaiming the port."""

    def __init__(self, fail_on):
        self.fail_on = fail_on  # "read" or "write"
        self.closed = False

    def read(self, size=1):
        if self.fail_on == "read":
            raise serial.SerialException("device reports readiness to read but returned no data")
        return b""

    def write(self, data):
        if self.fail_on == "write":
            raise serial.SerialException("could not write to port")
        return len(data)

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
