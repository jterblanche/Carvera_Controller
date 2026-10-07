"""Tests for WIFIStream socket write semantics."""

import socket
import struct
import time
from unittest.mock import MagicMock, patch

import pytest

import carveracontroller.Controller as controller_module
from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import PTYPE_FILE_DATA, build_frame
from carveracontroller.WIFIStream import MachineDetector, WIFIStream
from carveracontroller.XMODEM import XMODEM
from tests.unit.fake_machine import FakeMachine


class DeterministicShortWriteSocket:
    """Socket double that accepts only a prefix from each ``send`` call."""

    def __init__(self, short_write_size):
        self.short_write_size = short_write_size
        self.send_calls = 0
        self.sendall_calls = 0
        self.wire = bytearray()

    def send(self, data):
        self.send_calls += 1
        accepted = min(self.short_write_size, len(data))
        self.wire.extend(data[:accepted])
        return accepted

    def sendall(self, data):
        self.sendall_calls += 1
        self.wire.extend(data)


def _xmodem8k_frame():
    modem = XMODEM(lambda _size, _timeout=0.5: None, lambda data, _timeout=0.5: len(data))
    payload = b"x" * 8192
    framed_payload = bytes([len(payload) >> 8, len(payload) & 0xFF]) + payload
    return bytes(modem._make_send_header(8192, 1) + framed_payload + modem._make_send_checksum(1, framed_payload))


def _makera_file_data_frame():
    """Full Makera FILE_DATA frame as emitted by XMODEM.send() via putc."""
    seq = struct.pack(">I", 1)
    file_data = b"x" * 8192
    return build_frame(PTYPE_FILE_DATA, seq + file_data)


def _assert_putc_writes_complete_frame(frame):
    stream = WIFIStream.__new__(WIFIStream)
    stream.socket = DeterministicShortWriteSocket(short_write_size=2048)

    result = stream.putc(frame)

    assert len(stream.socket.wire) == len(frame)
    assert bytes(stream.socket.wire) == frame
    assert stream.socket.send_calls == 0
    assert stream.socket.sendall_calls == 1
    assert result == len(frame)


def test_putc_sends_the_complete_xmodem_frame_and_returns_its_length():
    frame = _xmodem8k_frame()
    assert len(frame) == 8199, "fixture must model a complete xmodem8k frame"
    _assert_putc_writes_complete_frame(frame)


def test_putc_sends_the_complete_makera_file_data_frame_and_returns_its_length():
    frame = _makera_file_data_frame()
    assert len(frame) == 8205, "fixture must model a complete Makera FILE_DATA frame"
    _assert_putc_writes_complete_frame(frame)


class TimesOutPartWaySocket:
    """Socket double that accepts ``accept_before_timeout`` bytes in total,
    a prefix at a time, then raises socket.timeout as a real socket with a
    timeout set does once the peer stops taking data."""

    def __init__(self, accept_before_timeout, chunk=2048):
        self.remaining = accept_before_timeout
        self.chunk = chunk
        self.wire = bytearray()
        self.shut_down = False

    def send(self, data):
        if self.remaining == 0:
            raise socket.timeout("timed out")
        accepted = min(self.chunk, self.remaining, len(data))
        self.remaining -= accepted
        self.wire.extend(data[:accepted])
        return accepted

    def shutdown(self, how):
        self.shut_down = True


def _stream_with(sock):
    stream = WIFIStream.__new__(WIFIStream)
    stream.log_sent_receive = False
    stream.socket = sock
    return stream


@pytest.mark.parametrize("frame", [_xmodem8k_frame(), _makera_file_data_frame()], ids=["xmodem8k", "makera"])
def test_send_writes_the_whole_frame_when_the_socket_takes_part_of_it_per_call(frame):
    stream = _stream_with(DeterministicShortWriteSocket(short_write_size=2048))

    stream.send(frame)

    assert bytes(stream.socket.wire) == frame


def test_send_that_times_out_part_way_through_a_frame_fails_the_link():
    """Part of the frame is on the wire and the rest never will be, so the
    byte stream is out of step: the send must raise, and the socket must be
    shut down so the reader sees the link as closed."""
    frame = _makera_file_data_frame()
    stream = _stream_with(TimesOutPartWaySocket(accept_before_timeout=3000))

    with pytest.raises(OSError, match="3000 of 8205"):
        stream.send(frame)

    assert bytes(stream.socket.wire) == frame[:3000]
    assert stream.socket.shut_down is True


def test_send_that_times_out_before_writing_anything_raises_and_keeps_the_link():
    """Nothing of the frame was written, so the byte stream is still in
    step: the timeout is raised as it is, and the link is left open."""
    stream = _stream_with(TimesOutPartWaySocket(accept_before_timeout=0))

    with pytest.raises(socket.timeout):
        stream.send(b"?")

    assert stream.socket.wire == bytearray()
    assert stream.socket.shut_down is False


def test_send_on_a_real_socket_whose_peer_stops_reading_never_cuts_a_frame_silently():
    """A real TCP socket with the stream's own timeout, and a peer that
    accepts the connection but never reads: once the kernel buffers fill,
    send() either has written the whole frame or has raised and shut the
    link down."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    peer = None
    try:
        client.connect(listener.getsockname())
        peer, _ = listener.accept()
        client.settimeout(0.05)
        stream = _stream_with(client)
        frame = b"x" * (4 * 1024 * 1024)

        with pytest.raises(OSError, match=f"of {len(frame)} bytes"):
            stream.send(frame)

        # Shut down for writing: a further send fails at once.
        with pytest.raises(OSError):
            client.send(b"y")
    finally:
        client.close()
        if peer is not None:
            peer.close()
        listener.close()


class _CutFrameSocket:
    """Wraps the controller's real socket. Once ``cut`` is set, the next
    frame gets three bytes onto the wire and then times out."""

    def __init__(self, real):
        self._real = real
        self.cut = False
        self._taken = None

    def send(self, data):
        if not self.cut:
            return self._real.send(data)
        if self._taken is None:
            self._taken = self._real.send(data[:3])
            return self._taken
        raise socket.timeout("timed out")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_frame_cut_short_on_a_working_wifi_link_is_reported_as_a_lost_connection():
    """End to end: a command sent while the link is up gets only part of
    its frame onto the wire. The controller logs the error, gives up the
    link and tells the user the connection was lost, the same as when the
    machine closes it."""
    machine = FakeMachine(mode="new")
    controller = Controller(CNC(), callback=None, identity=ControllerIdentity(id=0x0102030405060708, name="Test PC"))
    fake_app = MagicMock()
    try:
        with (
            patch.object(controller_module, "App") as mock_app_cls,
            patch.object(controller_module, "Clock") as mock_clock,
        ):
            mock_app_cls.get_running_app.return_value = fake_app
            controller.open(CONN_WIFI, machine.address())
            streamio_thread = controller.thread
            assert machine.wait_until(lambda: controller.comms.frame_confirmed is True, timeout=2.0)
            assert machine.wait_until(
                lambda: controller._hello is not None and controller._hello.identified, timeout=2.0
            )

            wrapped = _CutFrameSocket(controller.stream.socket)
            controller.stream.socket = wrapped
            wrapped.cut = True
            controller.executeCommand("G0 X10 Y10")

            assert machine.wait_until(lambda: controller.stream is None, timeout=2.0), (
                "controller must give up a link whose frame was cut short"
            )
            streamio_thread.join(timeout=2.0)
            assert not streamio_thread.is_alive()

            errors = []
            while not controller.log.empty():
                kind, text = controller.log.get_nowait()
                if kind == Controller.MSG_ERROR:
                    errors.append(text)
            assert any("bytes" in text for text in errors), errors

            for call in mock_clock.schedule_once.call_args_list:
                call.args[0](0)
            fake_app.root.show_peer_closed_popup.assert_called_once()
    finally:
        controller.close(allow_reconnect=False)
        machine.stop()


class _FakeUdpSocket:
    """recvfrom() double for MachineDetector.check_for_responses()."""

    def __init__(self, packets):
        self._packets = list(packets)

    def recvfrom(self, bufsize):
        if not self._packets:
            raise OSError("no more packets")
        return self._packets.pop(0), ("0.0.0.0", 0)

    def close(self):
        pass


def _detector_with_packet(payload: bytes) -> MachineDetector:
    detector = MachineDetector()
    detector.sock = _FakeUdpSocket([payload])
    detector.t = detector.tr = 0.0
    return detector


def test_beacon_fifth_field_parsed_as_old_controller_present():
    detector = _detector_with_packet(b"Carvera,10.0.0.5,2222,1,1")

    detector.check_for_responses()

    assert detector.machine_list == [
        {"machine": "Carvera", "ip": "10.0.0.5", "port": 2222, "busy": True, "old_controller_present": True}
    ]


def test_beacon_without_fifth_field_defaults_old_controller_present_false():
    detector = _detector_with_packet(b"Carvera,10.0.0.5,2222,1")

    detector.check_for_responses()

    assert detector.machine_list[0]["old_controller_present"] is False


def test_is_old_controller_present_looks_up_by_ip():
    detector = _detector_with_packet(b"Carvera,10.0.0.5,2222,1,1")
    detector.check_for_responses()

    assert detector.is_old_controller_present("10.0.0.5") is True
    assert detector.is_old_controller_present("10.0.0.9") is False


def test_a_repeated_beacon_within_one_scan_updates_the_entry_in_place():
    """A machine's old_controller_present flag can change mid-scan (e.g. an
    old controller connects while the discovery dropdown is still open) —
    a second beacon from the same machine name must refresh its entry, not
    be silently ignored because that name was already seen."""
    detector = MachineDetector()
    detector.sock = _FakeUdpSocket(
        [
            b"Carvera,10.0.0.5,2222,1,0",
            b"Carvera,10.0.0.5,2222,1,1",
        ]
    )
    # Real timestamps, not 0.0: check_for_responses() sets self.t to the
    # real clock on every call, so a second call needs self.tr to still be
    # within its 3s window relative to that, not stuck at a fixed 0.0.
    detector.t = detector.tr = time.time()

    detector.check_for_responses()
    detector.check_for_responses()

    assert len(detector.machine_list) == 1
    assert detector.machine_list[0]["old_controller_present"] is True


def test_two_machines_sharing_a_name_get_separate_entries_not_one_flip_flopping_one():
    """The in-place update is keyed by (name, ip) together, not name alone
    — two different machines that happen to broadcast the same name must
    not be folded into a single entry that alternates between their IPs."""
    detector = MachineDetector()
    detector.sock = _FakeUdpSocket(
        [
            b"Carvera,10.0.0.5,2222,0,0",
            b"Carvera,10.0.0.9,2222,1,1",
            b"Carvera,10.0.0.5,2222,0,0",
        ]
    )
    detector.t = detector.tr = time.time()

    detector.check_for_responses()
    detector.check_for_responses()
    detector.check_for_responses()

    assert len(detector.machine_list) == 2
    ips = {entry["ip"] for entry in detector.machine_list}
    assert ips == {"10.0.0.5", "10.0.0.9"}
