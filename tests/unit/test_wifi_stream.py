"""Tests for WIFIStream socket write semantics."""

import struct
import time

from carveracontroller.protocols.framing import PTYPE_FILE_DATA, build_frame
from carveracontroller.WIFIStream import MachineDetector, WIFIStream
from carveracontroller.XMODEM import XMODEM


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
