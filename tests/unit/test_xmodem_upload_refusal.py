"""A refused upload must surface the firmware's reason and end at once,
instead of silently discarding the refusal and only giving up once
XMODEM.send()'s own 9s receive-timeout fires with no reason at all.

The firmware's control gate (ControlToken::gate(), src/libs/ControlToken.cpp)
refuses an upload outright -- someone else has control, or motion is already
in progress -- and never starts the file transfer. Its reply
(WifiProvider::gate_dispatch()/printf()) is one line of text framed as an
ordinary PTYPE_NORMAL_INFO packet, the same type every "ok"/error reply uses
(see fake_machine.py's send_normal_info and tests/unit/test_passive_state.py).
XMODEM.send() ignores every reply type below its own file-transfer range
(``cmd_type < PTYPE_FILE_MD5``) without looking at it, so this reply used to
vanish into that `continue` and leave the caller waiting for file-transfer
packets that were never going to arrive.
"""

from __future__ import annotations

from io import BytesIO

from carveracontroller.protocols.framing import PTYPE_FILE_CAN, PTYPE_NORMAL_INFO, build_frame
from carveracontroller.XMODEM import XMODEM

REFUSED_HOLDER = b"error:Refused -- PC has control\r\n"
REFUSED_MOTION = b"error:Transfer refused -- an interactive move is in progress\r\n"


def _send_with_reply(reply_frame, timeout=1):
    """Feed one framed reply to XMODEM.send() as if received right after
    the upload's own MD5 announcement, and return (result, modem)."""
    transport = BytesIO(reply_frame)

    def getc(size, timeout=0.5):
        return transport.read(size) or None

    def putc(data, timeout=0.5):
        return len(data)

    modem = XMODEM(getc, putc, "xmodem8k")
    stream = BytesIO(b"G0 X1 Y2\nM2\n")
    result = modem.send(stream, md5="deadbeefdeadbeefdeadbeefdeadbeef", retry=5, timeout=timeout)
    return result, modem


def test_refused_upload_ends_at_once_with_the_holder_named():
    result, modem = _send_with_reply(build_frame(PTYPE_NORMAL_INFO, REFUSED_HOLDER))

    assert result is None
    assert modem.last_file_error == REFUSED_HOLDER.decode().strip()


def test_refused_upload_ends_at_once_without_a_named_holder():
    result, modem = _send_with_reply(build_frame(PTYPE_NORMAL_INFO, REFUSED_MOTION))

    assert result is None
    assert modem.last_file_error == REFUSED_MOTION.decode().strip()


def test_normal_info_that_is_not_a_refusal_is_still_ignored():
    """Anything else carried as PTYPE_NORMAL_INFO while waiting (a stray
    "ok", status text, ...) must still be skipped, not mistaken for a
    refusal. Proven by following it with an unrelated PTYPE_FILE_CAN: if
    the "ok" had been (wrongly) treated as a refusal, send() would have
    returned before ever reaching it, and last_file_error would carry the
    "ok" text instead of this one."""
    reply = build_frame(PTYPE_NORMAL_INFO, b"ok\r\n") + build_frame(PTYPE_FILE_CAN, b"some other reason")
    result, modem = _send_with_reply(reply)

    assert result is None
    assert modem.last_file_error == "some other reason"
