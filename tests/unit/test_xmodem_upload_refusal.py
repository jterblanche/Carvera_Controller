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
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from carveracontroller.Controller import SEND_FILE, Controller
from carveracontroller.machine.local_copies import LocalCopyStore
from carveracontroller.machine.passive_fetch import PassiveFetchTracker
from carveracontroller.main import Makera
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


# -- the machine's busy reply ------------------------------------------------------
#
# While the machine runs another controller's file transfer it discards this
# controller's commands and answers with its busy reply (machine/peer_transfer.py),
# also a PTYPE_NORMAL_INFO line. The transfer never starts, so waiting for it is
# pointless, and FILE_CAN would only be discarded and draw the busy reply again.

BUSY = b"error:Busy -- a file transfer is in progress, retry when it finishes\r\n"


def _modem_with_reply(reply_frame):
    """An XMODEM reading ``reply_frame`` and recording every frame it sends."""
    transport = BytesIO(reply_frame)
    sent = []

    def getc(size, timeout=0.5):
        return transport.read(size) or None

    def putc(data, timeout=0.5):
        sent.append(bytes(data))
        return len(data)

    return XMODEM(getc, putc, "xmodem8k"), sent


def _sent_types(sent):
    return [frame[4] for frame in sent]


def test_busy_reply_ends_an_upload_at_once_without_cancelling():
    modem, sent = _modem_with_reply(build_frame(PTYPE_NORMAL_INFO, BUSY))
    stream = BytesIO(b"G0 X1 Y2\nM2\n")

    result = modem.send(stream, md5="deadbeefdeadbeefdeadbeefdeadbeef", retry=5, timeout=1)

    assert result is None
    assert modem.last_file_error == BUSY.decode().strip()
    assert PTYPE_FILE_CAN not in _sent_types(sent)


def test_busy_reply_ends_a_download_at_once_without_cancelling():
    modem, sent = _modem_with_reply(build_frame(PTYPE_NORMAL_INFO, BUSY))

    result = modem.recv(BytesIO(), md5="", retry=5, timeout=1)

    assert result is None
    assert modem.last_file_error == BUSY.decode().strip()
    assert sent == []


def test_busy_reply_is_kept_for_the_controller():
    """The controller learns from it that the machine is busy with another
    controller's transfer, as it would had the reply come outside one."""
    modem, _sent = _modem_with_reply(build_frame(PTYPE_NORMAL_INFO, BUSY))

    modem.recv(BytesIO(), md5="", retry=5, timeout=1)

    kept = modem.take_other_frames()
    assert len(kept) == 1
    assert kept[0][2] == PTYPE_NORMAL_INFO
    assert BUSY.strip() in kept[0]


def test_refusal_is_still_not_kept_for_the_controller():
    """Unchanged: the control refusal is reported by the caller from
    last_file_error alone."""
    modem, _sent = _modem_with_reply(build_frame(PTYPE_NORMAL_INFO, REFUSED_HOLDER))

    modem.recv(BytesIO(), md5="", retry=5, timeout=1)

    assert modem.last_file_error == REFUSED_HOLDER.decode().strip()
    assert modem.take_other_frames() == []


# -- what the operator sees when an upload is turned away --------------------------


def _upload_host(tmp_path, last_file_error):
    """Makera.doUpload around a transfer that ended with ``last_file_error``."""
    root = Makera.__new__(Makera)
    root.temp_dir = str(tmp_path / "cache")
    root._passive_fetch = PassiveFetchTracker()
    root._local_copies = LocalCopyStore()
    root._uploading_firmware = False
    root.uploading = False
    root.decompstatus = False
    root.file_popup = SimpleNamespace(machine_dir="/sd/gcodes", refresh_machine=MagicMock())
    root.controller = SimpleNamespace(
        sendNUM=SEND_FILE,
        loadNUM=0,
        pauseStream=MagicMock(),
        resumeStream=MagicMock(),
        uploadCommand=MagicMock(return_value=True),
        stream=SimpleNamespace(
            upload=MagicMock(return_value=None), modem=SimpleNamespace(last_file_error=last_file_error)
        ),
        log=MagicMock(),
    )
    root.show_message_popup = MagicMock()
    root._cleanup_firmware_temp = MagicMock()
    job = tmp_path / "job.nc"
    job.write_bytes(b"G0 X0\n")
    root.uploading_file = str(job)
    root.original_upload_filepath = str(job)
    return root


@pytest.mark.parametrize(
    ("reason", "popup", "console"),
    [
        (BUSY.decode().strip(), BUSY.decode().strip(), []),
        (REFUSED_HOLDER.decode().strip(), "Upload refused: PC has control", [REFUSED_HOLDER.decode().strip()]),
        (
            REFUSED_MOTION.decode().strip(),
            "Upload refused: an interactive move is in progress",
            [REFUSED_MOTION.decode().strip()],
        ),
        ("Some other failure", "Upload file error!", ["Some other failure"]),
    ],
    ids=["busy", "refused", "refused-motion", "other"],
)
def test_upload_turned_away_shows_the_machines_reason_in_place_of_the_generic_error(
    tmp_path, monkeypatch, reason, popup, console
):
    """Turned away because another controller's transfer is running, the
    popup gives the machine's busy reply, which says to try again when it
    finishes. The controller has already put that reply on the console when
    it learned the machine was busy, so it is not repeated there. A control
    refusal gives who has control or what blocks the transfer, and is also
    put on the console. Any other failure keeps the generic text."""
    monkeypatch.setattr("carveracontroller.main.App.get_running_app", lambda: None)
    scheduled = []
    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", lambda cb, *a, **kw: scheduled.append(cb))
    root = _upload_host(tmp_path, reason)

    Makera.doUpload(root, None)

    popups = [cb for cb in scheduled if getattr(cb, "func", None) is root.show_message_popup]
    assert len(popups) == 1
    assert popups[0].args[0] == popup
    errors = [c.args[0][1] for c in root.controller.log.put.call_args_list if c.args[0][0] == Controller.MSG_ERROR]
    assert errors == console
