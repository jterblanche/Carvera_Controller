"""A framed download (XMODEM.recv) that hears nothing for a while asks again
for what it is waiting for by name, never with FILE_RETRY.

The machine's download loop (firmware Player::download_command) answers a
FILE_RETRY by resending its last packet and sets a flag, ``beretry``, that
only its data-request branch clears. While the flag is set, a data request's
sequence number is ignored and the last stored one is served instead, which
is 0 before any data has gone out. So a RETRY sent while the controller
waits for the digest or the file view makes the first data request seek to
before the start of the file; FatFs clips that to the end, the read comes
back empty, and the machine ends the download with FILE_CAN and "Error:
Machine read file error!". Over USB the controller's read times out after
0.3 seconds, so a machine a little slow to send the digest was enough: the
first config.txt download after switching from WiFi to USB ended that way.

``FirmwareDownload`` below plays that loop, flag included.
"""

from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

from carveracontroller.protocols.framing import (
    PTYPE_FILE_CAN,
    PTYPE_FILE_DATA,
    PTYPE_FILE_END,
    PTYPE_FILE_MD5,
    PTYPE_FILE_RETRY,
    PTYPE_FILE_VIEW,
    build_frame,
)
from carveracontroller.XMODEM import XMODEM

FILE = ("# config\n" + "".join(f"setting_{i} {i}\n" for i in range(60))).encode()
BUFSZ = 128  # what the firmware uses on USB


class FirmwareDownload:
    """The machine's side of a framed download, as Player::download_command
    runs it. ``silent_reads`` is how many of the controller's reads find
    nothing before the digest goes out (the machine still opening files)."""

    def __init__(self, data, silent_reads=0):
        self.data = data
        self.silent_reads = silent_reads
        self.out = bytearray()
        self.lastcmd = PTYPE_FILE_MD5
        self.beretry = False
        self.filesendseq = 0
        self.sent_by_controller = []
        self.ended = None
        self.error_msg = None

    # The controller's transport -------------------------------------------
    def getc(self, size, timeout=1):
        if not self.out:
            if self.silent_reads > 0:
                self.silent_reads -= 1
                if self.silent_reads == 0:
                    self._send(PTYPE_FILE_MD5, hashlib.md5(self.data).hexdigest().encode())
            return None
        chunk = bytes(self.out[:size])
        del self.out[:size]
        return chunk

    def putc(self, frame, timeout=1):
        cmd = frame[4]
        payload = bytes(frame[5:-4])
        self.sent_by_controller.append(cmd)
        if self.ended is None:
            self._handle(cmd, payload)
        return len(frame)

    def start(self):
        if self.silent_reads == 0:
            self._send(PTYPE_FILE_MD5, hashlib.md5(self.data).hexdigest().encode())
        return self

    # The firmware loop ----------------------------------------------------
    def _send(self, cmd, payload):
        self.out += build_frame(cmd, payload)

    def _handle(self, cmd, payload):
        if cmd == PTYPE_FILE_RETRY:
            cmd = self.lastcmd
            self.beretry = True
        if cmd == PTYPE_FILE_MD5:
            self._send(PTYPE_FILE_MD5, hashlib.md5(self.data).hexdigest().encode())
            self.lastcmd = PTYPE_FILE_MD5
        elif cmd == PTYPE_FILE_VIEW:
            packets = -(-len(self.data) // BUFSZ)
            self._send(PTYPE_FILE_VIEW, packets.to_bytes(4, "big") + BUFSZ.to_bytes(2, "big"))
            self.lastcmd = PTYPE_FILE_VIEW
        elif cmd == PTYPE_FILE_DATA:
            if not self.beretry:
                self.filesendseq = int.from_bytes(payload[:4], "big")
            offset = (self.filesendseq - 1) * BUFSZ
            if offset < 0:
                # f_lseek takes an unsigned offset and clips it to the file size.
                offset = len(self.data)
            chunk = self.data[offset : offset + BUFSZ]
            if not chunk:
                self.error_msg = "Error: Machine read file error!"
                self._send(PTYPE_FILE_CAN, b"ok\r\n")
                self.ended = "read error"
                return
            self._send(PTYPE_FILE_DATA, self.filesendseq.to_bytes(4, "big") + chunk)
            self.lastcmd = PTYPE_FILE_DATA
            self.beretry = False
        elif cmd == PTYPE_FILE_END:
            self._send(PTYPE_FILE_END, b"")
            self.ended = "success"
        elif cmd == PTYPE_FILE_CAN:
            self.ended = "canceled by controller"


def _download(machine):
    modem = XMODEM(machine.getc, machine.putc, "USBMode")
    out = BytesIO()
    result = modem.recv(out, md5="", retry=50)
    return result, out.getvalue(), modem


@pytest.mark.parametrize("silent_reads", [0, 1, 3])
def test_download_from_a_machine_slow_to_send_the_digest_completes(silent_reads):
    machine = FirmwareDownload(FILE, silent_reads=silent_reads).start()

    result, received, _modem = _download(machine)

    assert machine.error_msg is None
    assert result == len(FILE)
    assert received == FILE
    assert machine.ended == "success"


def test_a_download_never_sends_file_retry_while_waiting_for_the_digest():
    machine = FirmwareDownload(FILE, silent_reads=3).start()

    _download(machine)

    assert PTYPE_FILE_RETRY not in machine.sent_by_controller
    # The silence before the digest was answered by asking for the digest.
    assert machine.sent_by_controller[0] == PTYPE_FILE_MD5


class _Script:
    """A transport that plays fixed replies, with None for a read that times
    out, and records the controller's frames."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def getc(self, size, timeout=1):
        if not self.replies:
            return None
        reply = self.replies[0]
        if reply is None:
            self.replies.pop(0)
            return None
        chunk, rest = reply[:size], reply[size:]
        if rest:
            self.replies[0] = rest
        else:
            self.replies.pop(0)
        return chunk

    def putc(self, frame, timeout=1):
        self.sent.append((frame[4], bytes(frame[5:-4])))
        return len(frame)


def test_silence_while_waiting_for_the_file_view_asks_for_the_view_again():
    digest = hashlib.md5(b"abc").hexdigest().encode()
    script = _Script([build_frame(PTYPE_FILE_MD5, digest), None])
    modem = XMODEM(script.getc, script.putc, "USBMode")

    modem.recv(BytesIO(), md5="", retry=2)

    assert script.sent[0] == (PTYPE_FILE_VIEW, b"")
    assert script.sent[1] == (PTYPE_FILE_VIEW, b"")
    assert PTYPE_FILE_RETRY not in [cmd for cmd, _ in script.sent]


def test_silence_while_waiting_for_data_asks_for_that_packet_by_number():
    data = b"x" * 200
    digest = hashlib.md5(data).hexdigest().encode()
    view = (2).to_bytes(4, "big") + BUFSZ.to_bytes(2, "big")
    first = build_frame(PTYPE_FILE_DATA, (1).to_bytes(4, "big") + data[:BUFSZ])
    script = _Script([build_frame(PTYPE_FILE_MD5, digest), build_frame(PTYPE_FILE_VIEW, view), first, None])
    modem = XMODEM(script.getc, script.putc, "USBMode")

    modem.recv(BytesIO(), md5="", retry=2)

    # MD5 -> VIEW, VIEW -> DATA 1, DATA 1 -> DATA 2, silence -> DATA 2 again.
    assert script.sent[:4] == [
        (PTYPE_FILE_VIEW, b""),
        (PTYPE_FILE_DATA, (1).to_bytes(4, "big")),
        (PTYPE_FILE_DATA, (2).to_bytes(4, "big")),
        (PTYPE_FILE_DATA, (2).to_bytes(4, "big")),
    ]
    assert PTYPE_FILE_RETRY not in [cmd for cmd, _ in script.sent]


def test_firmware_model_shows_why_a_retry_before_data_ends_the_download():
    """The machine-side flaw this works around, in the model above: a RETRY
    answered with the digest leaves the flag set, and the first data request
    then reads past the end of the file."""
    machine = FirmwareDownload(FILE).start()
    machine.out.clear()
    machine.putc(build_frame(PTYPE_FILE_RETRY, b""))
    machine.putc(build_frame(PTYPE_FILE_VIEW, b""))
    machine.putc(build_frame(PTYPE_FILE_DATA, (1).to_bytes(4, "big")))

    assert machine.ended == "read error"
    assert machine.error_msg == "Error: Machine read file error!"
