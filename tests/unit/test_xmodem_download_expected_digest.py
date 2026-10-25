"""A download whose file digest is already known must not accept the digest
of another file.

The machine's digest frame names no file. If it is still serving an
abandoned download of file X when the controller asks for file Y, X's digest
and data arrive as the answer for Y, and the integrity check against that
same digest passes. A caller that knows Y's digest passes it as
``expected_md5`` so the mismatch ends the download.
"""

import hashlib
from io import BytesIO

from carveracontroller.machine.passive_fetch import PassiveFetchTracker
from carveracontroller.protocols.framing import (
    PTYPE_FILE_CAN,
    PTYPE_FILE_DATA,
    PTYPE_FILE_MD5,
    PTYPE_FILE_VIEW,
    build_frame,
)
from carveracontroller.XMODEM import XMODEM

WANTED = b"G0 X1 Y2\nM2\n"
STALE = b"G0 X9 Y9\nM2\n"
Z1_PLACEHOLDER_MD5 = b"default_md5_hash_value_32_bytes_"


def _receive(served, advertised, expected_md5):
    transport = BytesIO()
    sent = []

    def getc(size, timeout=0.5):
        return transport.read(size) or None

    def putc(data, timeout=0.5):
        sent.append(bytes(data))
        return len(data)

    modem = XMODEM(getc, putc, "xmodem8k")
    transport.write(build_frame(PTYPE_FILE_MD5, advertised))
    transport.write(build_frame(PTYPE_FILE_VIEW, (1).to_bytes(4, "big") + (8192).to_bytes(2, "big")))
    transport.write(build_frame(PTYPE_FILE_DATA, (1).to_bytes(4, "big") + served))
    transport.seek(0)
    output = BytesIO()
    result = modem.recv(output, retry=5, expected_md5=expected_md5)
    return result, output.getvalue(), sent, modem


def _md5(payload):
    return hashlib.md5(payload).hexdigest()


def test_another_files_digest_ends_the_download_and_cancels_it():
    result, output, sent, modem = _receive(STALE, _md5(STALE).encode(), _md5(WANTED))

    assert result is None
    assert output == b""
    assert any(frame[4] == PTYPE_FILE_CAN for frame in sent)
    assert not any(frame[4] == PTYPE_FILE_VIEW for frame in sent)
    assert "another file" in modem.last_file_error


def test_the_expected_digest_is_accepted_in_either_case():
    result, output, _sent, _modem = _receive(WANTED, _md5(WANTED).encode(), _md5(WANTED).upper())

    assert result is not None and result > 0
    assert output == WANTED


def test_without_an_expected_digest_nothing_changes():
    result, output, _sent, _modem = _receive(STALE, _md5(STALE).encode(), None)

    assert result is not None and result > 0
    assert output == STALE


def test_a_placeholder_digest_is_not_taken_for_another_file():
    result, output, _sent, _modem = _receive(WANTED, Z1_PLACEHOLDER_MD5, _md5(WANTED))

    assert result is not None and result > 0
    assert output == WANTED


def test_tracker_gives_the_digest_announced_for_that_path_only():
    tracker = PassiveFetchTracker()
    digest = bytes.fromhex(_md5(WANTED))

    assert tracker.expected_digest("/sd/gcodes/a.nc") is None
    tracker.checksum_for("/sd/gcodes/a.nc", digest)

    assert tracker.expected_digest("/sd/gcodes/a.nc") == _md5(WANTED)
    assert tracker.expected_digest("/sd/gcodes/b.nc") is None


def test_tracker_gives_nothing_for_an_event_without_a_digest():
    tracker = PassiveFetchTracker()
    tracker.checksum_for("/sd/gcodes/a.nc", b"")

    assert tracker.expected_digest("/sd/gcodes/a.nc") is None
