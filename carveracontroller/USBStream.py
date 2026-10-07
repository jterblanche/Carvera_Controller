import errno
import logging
import re
import time

import serial

from .machine.peer_closed import PeerClosedError
from .XMODEM import XMODEM

logger = logging.getLogger(__name__)

SERIAL_TIMEOUT = 0.3  # s

# How pyserial reports that the USB device behind an already open port has
# gone away (cable unplugged, machine powered off). This only describes
# failures on an open port: the same Windows "Access is denied" from
# opening a port means another program holds it, which is a real error.
#
# Windows (serialwin32) raises SerialException("<call> failed (<repr of
# ctypes.WinError()>)"), so only the text survives. The Windows error code
# is the last number in it; the message before it is localised.
#   5     ERROR_ACCESS_DENIED, what ClearCommError returns once the cable is out
#   22    ERROR_BAD_COMMAND, "The device does not recognize the command"
#   31    ERROR_GEN_FAILURE, "A device attached to the system is not functioning"
#   1167  ERROR_DEVICE_NOT_CONNECTED
_WINDOWS_CALL_FAILED = re.compile(r"^(?:ClearCommError|ReadFile|WriteFile|GetOverlappedResult) failed \(.*, (\d+)\)\)$")
_WINDOWS_DEVICE_GONE = frozenset({5, 22, 31, 1167})
# Linux and macOS (serialposix): in_waiting is a TIOCINQ query that raises
# OSError as it is; read() and write() wrap it as "read failed: [Errno n]
# ..." or "write failed: [Errno n] ...". A tty whose device has gone
# answers EIO (Linux) or ENXIO (macOS), and ENODEV once the node is gone.
_POSIX_CALL_FAILED = re.compile(r"^(?:read|write) failed: \[Errno (\d+)\]")
_POSIX_DEVICE_GONE = frozenset({errno.EIO, errno.ENXIO, errno.ENODEV})
# serialposix read() when select() reports the port readable but read()
# returns nothing, which is what a tty does once its device has gone.
_NO_DATA_WHEN_READY = "device reports readiness to read but returned no data"


def is_device_gone(exc):
    """True when exc is pyserial reporting that the device behind an open
    port has gone away, False for any other failure."""
    if isinstance(exc, serial.SerialException):
        text = str(exc)
        if text.startswith(_NO_DATA_WHEN_READY):
            return True
        match = _WINDOWS_CALL_FAILED.match(text)
        if match:
            return int(match.group(1)) in _WINDOWS_DEVICE_GONE
        match = _POSIX_CALL_FAILED.match(text)
        return bool(match) and int(match.group(1)) in _POSIX_DEVICE_GONE
    return isinstance(exc, OSError) and exc.errno in _POSIX_DEVICE_GONE


# ==============================================================================
# USB stream class
# ==============================================================================
class USBStream:
    serial = None
    resets_on_open = True
    supports_baud = True

    # ----------------------------------------------------------------------
    def __init__(self, log_sent_receive=False):
        self.modem = XMODEM(self.getc, self.putc, "xmodem")
        # Rely on the app/Kivy root logger; do not attach extra StreamHandlers to
        # the shared "xmodem.XMODEM" logger (USB+WiFi would duplicate every line).
        self.log_sent_receive = log_sent_receive
        self._recv_log_buffer = b""
        # Set by Controller when the communication protocol is selected.
        self.uses_framed_transfer = False

    # ----------------------------------------------------------------------
    def send(self, data):
        if self.serial is None:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        try:
            self.serial.write(data)
        except serial.SerialException as exc:
            self._fail(exc)
            if is_device_gone(exc):
                return
            raise PeerClosedError(str(exc)) from exc
        if self.log_sent_receive:
            # One line per write, holding exactly the bytes just written.
            logger.debug("SENT: %r", data)

    # ----------------------------------------------------------------------
    def recv(self):
        if self.serial is None:
            return b""
        try:
            data = self.serial.read()
        except serial.SerialException as exc:
            self._fail(exc)
            if is_device_gone(exc):
                return b""
            raise PeerClosedError(str(exc)) from exc
        if self.log_sent_receive and data:
            self._recv_log_buffer += data
            while b"\n" in self._recv_log_buffer:
                idx = self._recv_log_buffer.index(b"\n") + 1
                line = self._recv_log_buffer[:idx]
                self._recv_log_buffer = self._recv_log_buffer[idx:]
                logger.debug("RECV: %s", line.decode("utf-8", errors="replace").rstrip("\r\n"))
            if len(self._recv_log_buffer) > 4096:
                logger.debug("RECV: <%d bytes (no newline)>", len(self._recv_log_buffer))
                self._recv_log_buffer = b""
        return data

    # ----------------------------------------------------------------------
    def open(self, address, baud=115200):
        self._address = address.replace("\\", "\\\\")
        self.serial = serial.serial_for_url(
            self._address,
            baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=SERIAL_TIMEOUT,
            write_timeout=SERIAL_TIMEOUT,
            xonxoff=False,
            rtscts=False,
        )
        # Toggle DTR to reset Arduino
        try:
            self.serial.setDTR(0)
        except OSError:
            pass
        time.sleep(0.5)

        self.serial.flushInput()
        try:
            self.serial.setDTR(1)
        except OSError:
            pass
        time.sleep(0.5)

        # Try to clear machine receive buffer from a previous controller session
        self.serial.write(b"\n;\n")

        return True

    # ----------------------------------------------------------------------
    def reopen_at_baud(self, baud):
        """
        Close and reopen the serial port at a new baud rate.
        """
        if not self._address:
            return False
        baud = int(baud)
        old = self.serial
        self.serial = None
        self._recv_log_buffer = b""
        if old is not None:
            try:
                old.dtr = False
                old.rts = False
            except Exception:
                pass
            try:
                old.close()
            except Exception:
                pass
        time.sleep(0.1)
        try:
            ser = serial.serial_for_url(
                self._address,
                baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=SERIAL_TIMEOUT,
                write_timeout=SERIAL_TIMEOUT,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
                do_not_open=True,
            )
            try:
                ser.dtr = False
                ser.rts = False
            except Exception:
                pass
            ser.open()
            try:
                ser.dtr = False
            except Exception:
                pass
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
                pass
            self.serial = ser
            return True
        except Exception:
            logger.exception("Failed to reopen serial at %s baud", baud)
            return False

    # ----------------------------------------------------------------------
    def _fail(self, exc):
        """Tear down a serial port that just proved it's gone (read or
        write raised), without close()'s deliberate 0.5s settle delay —
        that delay is for an intentional close, and only slows down
        reporting a link that has already failed.

        A device that went away is an ordinary disconnect, not an error:
        the caller reports nothing to read or send, no status arrives, and
        the app's connection-lost check reports it and offers to reconnect
        as for any other lost link. Its exception is kept in the debug log
        only."""
        if is_device_gone(exc):
            logger.debug("USB device gone: %s", exc)
        else:
            logger.error("USB link failed: %s", exc)
        if self.serial is not None:
            try:
                self.serial.close()
            except Exception:
                pass
        self.serial = None
        self._recv_log_buffer = b""

    # ----------------------------------------------------------------------
    def close(self):
        if self.serial is None:
            return None
        time.sleep(0.5)
        try:
            self.modem.clear_mode_set()
            self.serial.close()
        except:
            pass
        self.serial = None
        self._recv_log_buffer = b""
        return True

    # ----------------------------------------------------------------------
    def waiting_for_send(self):
        if self.serial is None:
            return False
        return self.serial.out_waiting < 1

    # ----------------------------------------------------------------------
    def waiting_for_recv(self):
        if self.serial is None:
            return 0
        try:
            return self.serial.in_waiting
        except OSError as exc:
            # serial.SerialException is an OSError too. Any other failure
            # is raised unchanged for the caller to report.
            if not is_device_gone(exc):
                raise
            self._fail(exc)
            return 0

    # ----------------------------------------------------------------------
    def getc(self, size, timeout=1):
        if self.serial is None:
            return None
        return self.serial.read(size) or None

    def putc(self, data, timeout=1):
        if self.serial is None:
            return None
        return self.serial.write(data) or None

    def upload(self, filename, local_md5, callback):
        stream = open(filename, "rb")
        if self.uses_framed_transfer:
            result = self.modem.send(stream, md5=local_md5, retry=50, callback=callback)
        else:
            result = self.modem.send_legacy(stream, md5=local_md5, retry=10, callback=callback)
        stream.close()
        return result

    def download(self, filename, local_md5, callback):
        stream = open(filename, "wb")
        if self.uses_framed_transfer:
            result = self.modem.recv(stream, md5=local_md5, retry=50, callback=callback)
        else:
            result = self.modem.recv_legacy(stream, md5=local_md5, retry=10, callback=callback)
        stream.close()
        return result

    def cancel_process(self):
        self.modem.canceled = True
