"""Tests for what each stream's send() writes to the link and what its
SENT debug line reports.

Makera frames are binary and carry no line terminator, so a 0x0A byte turns
up in them only by chance (inside a length or checksum byte, or at the end
of a file-command payload). A frame sequence such as a job start followed by
heartbeats and then a file command is the shape that matters: the job-start
frame must reach the link exactly once, when it is sent, and every SENT
line must report the bytes of the one send() call that logged it.
"""

import logging
from types import SimpleNamespace

import pytest

from carveracontroller.protocols.makera import MakeraProtocol, encode_heartbeat
from carveracontroller.USBBulkStream import USBBulkStream
from carveracontroller.USBStream import USBStream
from carveracontroller.WIFIStream import WIFIStream


class _RecordingSerial:
    """serial.Serial double that keeps every byte written to it."""

    def __init__(self):
        self.wire = bytearray()

    def write(self, data):
        self.wire.extend(data)
        return len(data)


class _RecordingBulkDevice:
    """pyusb device double that keeps every byte written to its OUT endpoint."""

    def __init__(self):
        self.wire = bytearray()

    def write(self, endpoint, data, timeout=None):
        self.wire.extend(data)
        return len(data)


class _RecordingSocket:
    """socket double that keeps every byte sent on it."""

    def __init__(self):
        self.wire = bytearray()

    def send(self, data):
        self.wire.extend(data)
        return len(data)


def _usb_serial_stream(log):
    stream = USBStream(log_sent_receive=log)
    fake = _RecordingSerial()
    stream.serial = fake
    return stream, fake


def _usb_bulk_stream(log):
    stream = USBBulkStream(log_sent_receive=log)
    fake = _RecordingBulkDevice()
    stream.dev = fake
    stream.ep_out = SimpleNamespace(wMaxPacketSize=64, bEndpointAddress=0x01)
    return stream, fake


def _wifi_stream(log):
    stream = WIFIStream(log_sent_receive=log)
    fake = _RecordingSocket()
    stream.socket = fake
    return stream, fake


STREAMS = {
    "usb-serial": _usb_serial_stream,
    "usb-bulk": _usb_bulk_stream,
    "wifi": _wifi_stream,
}

PLAY = b"play /sd/gcodes/job.nc"


def _job_start_heartbeats_then_file_command():
    """A job start, idle heartbeats, then the file command a settings
    read-back sends. Only the last frame contains a 0x0A byte."""
    codec = MakeraProtocol()
    play = codec.encode_command(PLAY)
    heartbeat = encode_heartbeat()
    download = codec.encode_file_command(b"download /sd/config.txt")
    assert b"\n" not in play
    assert b"\n" not in heartbeat
    assert b"\n" in download
    return [play, heartbeat, heartbeat, download]


def _sent_messages(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("SENT:")]


@pytest.mark.parametrize("log", [False, True], ids=["log-off", "log-on"])
@pytest.mark.parametrize("make_stream", STREAMS.values(), ids=STREAMS.keys())
def test_each_send_writes_only_its_own_frame_and_the_job_start_reaches_the_link_once(make_stream, log):
    stream, fake = make_stream(log)
    frames = _job_start_heartbeats_then_file_command()

    for frame in frames:
        before = len(fake.wire)
        stream.send(frame)
        assert bytes(fake.wire[before:]) == frame

    assert bytes(fake.wire) == b"".join(frames)
    assert fake.wire.count(PLAY) == 1


@pytest.mark.parametrize("make_stream", STREAMS.values(), ids=STREAMS.keys())
def test_each_sent_line_reports_exactly_the_bytes_of_its_own_send(make_stream, caplog):
    caplog.set_level(logging.DEBUG)
    stream, _fake = make_stream(True)
    frames = _job_start_heartbeats_then_file_command()

    for frame in frames:
        caplog.clear()
        stream.send(frame)
        assert _sent_messages(caplog) == [f"SENT: {frame!r}"]


@pytest.mark.parametrize("make_stream", STREAMS.values(), ids=STREAMS.keys())
def test_a_text_line_is_reported_once_when_it_is_sent(make_stream, caplog):
    caplog.set_level(logging.DEBUG)
    stream, _fake = make_stream(True)

    stream.send(b"?")
    stream.send(b"echo echo\n")

    assert _sent_messages(caplog) == ["SENT: b'?'", "SENT: b'echo echo\\n'"]


@pytest.mark.parametrize("make_stream", STREAMS.values(), ids=STREAMS.keys())
def test_nothing_is_logged_when_logging_is_off(make_stream, caplog):
    caplog.set_level(logging.DEBUG)
    stream, _fake = make_stream(False)

    for frame in _job_start_heartbeats_then_file_command():
        stream.send(frame)

    assert _sent_messages(caplog) == []


class _ShortWriteSocket:
    """socket double whose send() takes only the first few bytes, as a
    socket with a send timeout may."""

    def __init__(self, accept):
        self.accept = accept
        self.wire = bytearray()

    def send(self, data):
        taken = data[: self.accept]
        self.wire.extend(taken)
        return len(taken)


class _FailingSocket:
    def send(self, data):
        raise OSError("Broken pipe")


def test_wifi_sent_line_reports_only_the_bytes_the_socket_took(caplog):
    caplog.set_level(logging.DEBUG)
    stream = WIFIStream(log_sent_receive=True)
    stream.socket = _ShortWriteSocket(accept=4)
    frame = MakeraProtocol().encode_command(PLAY)

    stream.send(frame)

    assert _sent_messages(caplog) == [f"SENT: {frame[:4]!r}"]


def test_wifi_send_that_fails_logs_no_sent_line(caplog):
    caplog.set_level(logging.DEBUG)
    stream = WIFIStream(log_sent_receive=True)
    stream.socket = _FailingSocket()

    with pytest.raises(OSError):
        stream.send(MakeraProtocol().encode_command(PLAY))

    assert _sent_messages(caplog) == []
