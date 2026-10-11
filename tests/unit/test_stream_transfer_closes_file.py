"""Every link's upload() and download() close the file they opened, however
the transfer ends.

When the serial port was closed under a running download on Windows, the
modem raised ('NoneType' object has no attribute 'hEvent') and
USBStream.download left its file open. Windows does not let a file that is
open be deleted or renamed, so the controller's clean-up of the temporary
file failed (WinError 32), and so did the next config download, which
could not rename its finished file into place.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from carveracontroller.USBBulkStream import USBBulkStream
from carveracontroller.USBStream import USBStream
from carveracontroller.WIFIStream import WIFIStream


class _LinkGone(Exception):
    pass


def _stream(cls, framed, opened):
    def transfer(stream, **_kwargs):
        opened.append(stream)
        raise _LinkGone("'NoneType' object has no attribute 'hEvent'")

    link = cls.__new__(cls)
    link.uses_framed_transfer = framed
    link.modem = SimpleNamespace(send=transfer, send_legacy=transfer, recv=transfer, recv_legacy=transfer)
    return link


@pytest.mark.parametrize("cls", [USBStream, WIFIStream, USBBulkStream])
@pytest.mark.parametrize("framed", [True, False])
@pytest.mark.parametrize("direction", ["upload", "download"])
def test_file_is_closed_when_the_transfer_raises(tmp_path, cls, framed, direction):
    path = tmp_path / "config.txt.tmp"
    path.write_bytes(b"old")
    opened = []
    link = _stream(cls, framed, opened)

    with pytest.raises(_LinkGone):
        getattr(link, direction)(str(path), "", None)

    assert len(opened) == 1
    assert opened[0].closed


@pytest.mark.parametrize("cls", [USBStream, WIFIStream, USBBulkStream])
def test_download_returns_the_modems_result_and_closes_the_file(tmp_path, cls):
    path = tmp_path / "job.nc"
    opened = []

    def recv(stream, **_kwargs):
        opened.append(stream)
        stream.write(b"G0 X1\n")
        return 6

    link = cls.__new__(cls)
    link.uses_framed_transfer = True
    link.modem = SimpleNamespace(recv=recv)

    assert link.download(str(path), "", None) == 6
    assert opened[0].closed
    assert path.read_bytes() == b"G0 X1\n"
