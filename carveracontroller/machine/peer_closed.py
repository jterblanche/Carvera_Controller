"""Shared signal for "the underlying link is gone".

USBStream and WIFIStream tell a working link apart from one whose peer has
gone in different ways, because the two transports don't fail the same way:

  - WiFi: a TCP ``recv()`` on a socket that has a receive timeout set
    returns ``b""`` immediately once the peer has closed the connection,
    and raises ``socket.timeout`` instead when there is simply nothing to
    read yet. That distinction is already an unambiguous plain return
    value, so WIFIStream keeps it as one rather than wrapping it in this
    exception — see the WiFi branch in Controller.streamIO.
  - USB: there is no equivalent "still open, nothing to read" vs "closed"
    return value. A gone device (unplugged, or the OS reclaiming the port)
    fails the read or write itself, as a ``serial.SerialException``.
    USBStream catches that and raises this instead, so Controller.streamIO
    has one common signal to catch regardless of which transport raised
    it.
"""


class PeerClosedError(Exception):
    """The stream's underlying link is gone: the peer closed it (WiFi), or
    the device itself disappeared (USB)."""
