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
    return value. A failing port fails the read or write itself, as a
    ``serial.SerialException``. When pyserial's error says the device has
    gone away (unplugged, machine powered off), USBStream tears the port
    down quietly instead, an ordinary disconnect that the app's
    connection-lost check reports (see ``USBStream.is_device_gone``). For
    any other read or write failure it raises this, so Controller.streamIO
    has one common signal to catch regardless of which transport raised
    it.
"""


class PeerClosedError(Exception):
    """The stream's underlying link is gone: the peer closed it (WiFi), or
    the device itself disappeared (USB)."""
