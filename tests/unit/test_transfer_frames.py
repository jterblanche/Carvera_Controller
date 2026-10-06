"""Frames that are not part of a file transfer.

While a framed upload or download runs, XMODEM.send()/recv() read the link
themselves (streamIO is paused), so every frame the machine sends in that
time passes through them first: status reports, published events
(control-changed, upload-finished, ...), published console lines, and
ordinary replies. Those must still reach the controller's normal handlers.

These tests drive a real Controller and its real WIFIStream/XMODEM over a
real socket against FakeMachine, which plays the machine's side of each
transfer through its frame hook.
"""

from __future__ import annotations

import hashlib

import pytest

from carveracontroller.CNC import CNC
from carveracontroller.Controller import CONN_WIFI, Controller
from carveracontroller.machine.identity import ControllerIdentity
from carveracontroller.protocols.framing import (
    PTYPE_AUTO_COMMAND,
    PTYPE_EVENT,
    PTYPE_FILE_DATA,
    PTYPE_FILE_END,
    PTYPE_FILE_MD5,
    PTYPE_FILE_START,
    PTYPE_FILE_VIEW,
    PTYPE_NORMAL_INFO,
    PTYPE_PUBLISHED_LINE,
    PTYPE_STATUS_RES,
    build_frame,
)
from carveracontroller.protocols.handshake import EVENT_KIND_CONTROL_CHANGED
from tests.unit.fake_machine import FakeMachine

IDENTITY = ControllerIdentity(id=0x0102030405060708, name="Test PC")
OTHER_ID = 0x1111222233334444
FILE_BYTES = b"G0 X1 Y2\nG1 X3 Y4 F100\nM2\n"


def _control_changed(holder_id, holder_name):
    payload = (
        bytes([EVENT_KIND_CONTROL_CHANGED]) + holder_id.to_bytes(8, "big") + bytes([len(holder_name)]) + holder_name
    )
    return build_frame(PTYPE_EVENT, payload)


def _published_line(source_id, source_name, text):
    payload = source_id.to_bytes(8, "big") + bytes([len(source_name)]) + source_name + bytes([0]) + text
    return build_frame(PTYPE_PUBLISHED_LINE, payload)


def _is_download_request(ptype, payload):
    """The download command, sent either plainly (FILE_START) or wrapped
    as an automatic command (kind 1), as the passive fetch sends it."""
    if ptype == PTYPE_FILE_START:
        return payload.startswith(b"download")
    if ptype == PTYPE_AUTO_COMMAND:
        return payload[:1] == b"\x01" and payload[1:].startswith(b"download")
    return False


class UploadPeer:
    """The machine's side of a framed upload. ``extra`` maps a stage to
    frames sent along with the machine's own packet at that point:
    "before" right after the upload command arrives (before any transfer
    packet), "during" in the same write as the request for the file's
    details, "after" in the same write as the end-of-transfer packet."""

    def __init__(self, extra):
        self.extra = extra
        self.machine = None
        self.total = 0
        self.received = bytearray()

    def __call__(self, conn, ptype, payload):
        send = self.machine.send_frame
        if ptype == PTYPE_FILE_START and payload.startswith(b"upload"):
            if self.extra.get("before"):
                send(self.extra["before"])
            return True
        if ptype == PTYPE_FILE_MD5:
            send(self.extra.get("during", b"") + build_frame(PTYPE_FILE_VIEW, b""))
            return True
        if ptype == PTYPE_FILE_VIEW:
            self.total = int.from_bytes(payload[:4], "big")
            send(build_frame(PTYPE_FILE_DATA, (1).to_bytes(4, "big")))
            return True
        if ptype == PTYPE_FILE_DATA:
            seq = int.from_bytes(payload[:4], "big")
            self.received.extend(payload[4:])
            if seq < self.total:
                send(build_frame(PTYPE_FILE_DATA, (seq + 1).to_bytes(4, "big")))
            else:
                send(build_frame(PTYPE_FILE_END, b"") + self.extra.get("after", b""))
            return True
        return False


class DownloadPeer:
    """The machine's side of a framed download of FILE_BYTES, in one
    packet. ``extra`` as for UploadPeer: "before" in the same write as the
    MD5 announcement, "during" in the same write as the file's details,
    "after" once the controller has sent its end-of-transfer packet."""

    def __init__(self, extra):
        self.extra = extra
        self.machine = None

    def __call__(self, conn, ptype, payload):
        send = self.machine.send_frame
        if _is_download_request(ptype, payload):
            md5 = hashlib.md5(FILE_BYTES).hexdigest().encode()
            send(self.extra.get("before", b"") + build_frame(PTYPE_FILE_MD5, md5))
            return True
        if ptype == PTYPE_FILE_VIEW:
            details = (1).to_bytes(4, "big") + (8192).to_bytes(2, "big")
            send(self.extra.get("during", b"") + build_frame(PTYPE_FILE_VIEW, details))
            return True
        if ptype == PTYPE_FILE_DATA:
            seq = int.from_bytes(payload[:4], "big")
            send(build_frame(PTYPE_FILE_DATA, seq.to_bytes(4, "big") + FILE_BYTES))
            return True
        if ptype == PTYPE_FILE_END:
            if self.extra.get("after"):
                send(self.extra["after"])
            return True
        return False


@pytest.fixture
def machines():
    instances = []
    yield instances
    for m in instances:
        m.stop()


@pytest.fixture
def controller():
    c = Controller(CNC(), callback=None, identity=IDENTITY)
    yield c
    c.close(allow_reconnect=False)


def _connect(machines, controller, peer, **kwargs):
    m = FakeMachine(mode="new", frame_hook=peer, **kwargs)
    peer.machine = m
    machines.append(m)
    controller.open(CONN_WIFI, m.address())
    assert m.wait_until(lambda: controller._hello is not None and controller._hello.identified, timeout=2.0)
    assert m.wait_until(lambda: controller.comms.uses_framed_transfer, timeout=2.0)
    return m


def _drain_log(controller):
    messages = []
    while True:
        try:
            messages.append(controller.log.get_nowait())
        except Exception:
            break
    return messages


def _upload(controller, tmp_path):
    """The same steps main.py's doUpload takes around the transfer."""
    local = tmp_path / "job.nc"
    local.write_bytes(FILE_BYTES)
    controller.pauseStream(0.0)
    assert controller.uploadCommand("/sd/gcodes/job.nc")
    result = controller.stream.upload(str(local), hashlib.md5(FILE_BYTES).hexdigest(), None)
    controller.resumeStream()
    return result


def _download(controller, tmp_path, automatic=False):
    """The same steps main.py's doDownload takes around a framed transfer."""
    controller.pauseStream(0.0)
    assert controller.downloadCommand("/sd/gcodes/job.nc", automatic=automatic)
    try:
        return controller.stream.download(str(tmp_path / "job.nc.tmp"), "", None)
    finally:
        controller.resumeStream()


# -- (a) frames around a transfer reach their normal handlers ------------------


@pytest.mark.parametrize("stage", ["before", "during", "after"])
def test_control_changed_event_around_an_upload_is_dispatched(machines, controller, tmp_path, stage):
    """The machine publishes control-changed to the uploader while its
    upload is still running. Wherever it lands relative to the transfer,
    the uploader must learn that it now holds control."""
    peer = UploadPeer({stage: _control_changed(IDENTITY.id, b"Test PC")})
    m = _connect(machines, controller, peer)
    assert controller.has_control is False

    assert _upload(controller, tmp_path) is True
    assert bytes(peer.received) == FILE_BYTES

    assert m.wait_until(lambda: controller.has_control, timeout=2.0)
    assert controller.control_holder_name == "Test PC"


@pytest.mark.parametrize("stage", ["before", "during", "after"])
def test_control_changed_event_around_a_download_is_dispatched(machines, controller, tmp_path, stage):
    peer = DownloadPeer({stage: _control_changed(OTHER_ID, b"Other")})
    m = _connect(machines, controller, peer)

    assert _download(controller, tmp_path) == len(FILE_BYTES)

    assert m.wait_until(lambda: controller.control_holder_id == OTHER_ID, timeout=2.0)
    assert controller.control_holder_name == "Other"


def test_published_line_status_and_reply_during_an_upload_are_dispatched(machines, controller, tmp_path, monkeypatch):
    """Everything else the machine sends mid-transfer, in order: another
    controller's published console line, a status report, and an ordinary
    reply to this controller."""
    during = (
        _published_line(OTHER_ID, b"Other", b"M114")
        + build_frame(PTYPE_STATUS_RES, b"<Alarm|MPos:1.0000,2.0000,3.0000,0.0000,0.0000>")
        + build_frame(PTYPE_NORMAL_INFO, b"ftype = lz\r\n")
    )
    peer = UploadPeer({"during": during})
    m = _connect(machines, controller, peer)
    monkeypatch.setitem(CNC.vars, "state", "N/A")
    _drain_log(controller)

    assert _upload(controller, tmp_path) is True

    assert m.wait_until(lambda: CNC.vars.get("state") == "Alarm", timeout=2.0)
    seen = _drain_log(controller)
    texts = [text for _kind, text in seen]
    assert (Controller.MSG_PUBLISHED, "[Other] M114") in seen
    assert "ftype = lz" in texts
    assert texts.index("[Other] M114") < texts.index("ftype = lz")
