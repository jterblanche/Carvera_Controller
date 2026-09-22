"""A fake machine: a real TCP socket speaking the Makera framed protocol.

Used to test the controller's connect and identify-handshake behaviour
end to end (real sockets, real background threads) without hardware.
Complements the existing client-side socket doubles
(tests/unit/test_wifi_stream.py) with a real accept()-ing server, since
these tests exercise Controller's own socket-based streamIO thread rather
than a single stream method.

Firmware personalities modelled:
  "new"      - a firmware that understands the identify handshake: acks
               hello (accepted, or rejected per ``ack_result``) and answers
               client-list requests.
  "old"      - today's firmware: silently drops every new message type
               (never even reads far enough to notice they're a hello or a
               client-list request), but still answers realtime status
               queries exactly as before.
  "silent"   - answers nothing at all, ever (a dead/unresponsive link) —
               used to prove hello is withheld until a frame is confirmed.
  "smoothie" - a machine still running the legacy plaintext line protocol:
               answers the controller's plaintext "echo" probe so the
               protocol detector picks Smoothie mode, never speaks framed
               bytes at all, and so never sees a hello either.

``close_after`` models a race observed against old firmware with another
client already attached: the machine accepts the TCP connection, then
closes it unconditionally after that many seconds without answering
anything at all — an accept immediately followed by a close, not a normal
"stayed connected but never answered" case.

``close_after_ack`` models the shape actually measured on hardware for an
eviction: the machine accepts a hello, acks it, keeps working normally
(answering polls) for a while, and then closes the link out from under a
session that had been fine — because another client has since identified
and this one hadn't (today's controller always identifies, so this is
standing in for the case where something else closes an established link
that already worked; the mechanism a real eviction uses to decide *when*
to close isn't reproduced here, only the shape of the close itself).

``publish_status=True`` stands in for the firmware's own proactive status
publish: once "new" mode accepts a hello, a background thread sends an
unsolicited PTYPE_STATUS_RES frame to that client every
``status_interval_s`` seconds, same as an identified client
would receive without polling. ``publish_status()`` and
``send_published_line()`` let a test send one of either on demand, to the
currently-connected client (there is only ever one in these tests) —
standing in for the machine relaying another controller's command/reply as
a PTYPE_PUBLISHED_LINE frame.
"""

from __future__ import annotations

import socket
import threading
import time

from carveracontroller.protocols.framing import (
    PTYPE_CLIENT_LIST_REPLY,
    PTYPE_CLIENT_LIST_REQ,
    PTYPE_CTRL_SINGLE,
    PTYPE_EVENT,
    PTYPE_HELLO,
    PTYPE_HELLO_ACK,
    PTYPE_NORMAL_INFO,
    PTYPE_PUBLISHED_LINE,
    PTYPE_RELAY,
    PTYPE_STATUS_RES,
    build_frame,
    validate_packet_data,
)
from carveracontroller.protocols.handshake import (
    EVENT_KIND_CONTROL_CHANGED,
    EVENT_KIND_PLAY_STARTED,
    EVENT_KIND_UPLOAD_FINISHED,
    HELLO_ACCEPTED,
)

_HEADER = bytes([0x86, 0x68])

DEFAULT_STATUS = b"<Idle,MPos:0.000,0.000,0.000,WPos:0.000,0.000,0.000>"


class FakeMachine:
    def __init__(
        self,
        mode="new",
        ack_delay=0.0,
        ack_result=HELLO_ACCEPTED,
        close_after=None,
        close_after_ack=None,
        publish_status=False,
        status_interval_s=0.05,
    ):
        self.mode = mode
        self.ack_delay = ack_delay
        self.ack_result = ack_result
        self.close_after = close_after
        self.close_after_ack = close_after_ack
        self.publish_status_enabled = publish_status
        self.status_interval_s = status_interval_s

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.hellos_received = []  # list[bytes]: raw hello payloads
        self.frames_received = []  # list[tuple[int, bytes]]: (ptype, payload)
        self.client_list_requests = 0
        self.smoothie_bytes_received = bytearray()  # "smoothie" mode only
        self._active_conn = None  # the one connected client's socket, or None

        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def address(self):
        return f"127.0.0.1:{self.port}"

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    # -- server internals ---------------------------------------------

    def _accept_loop(self):
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        if self.close_after is not None:
            time.sleep(self.close_after)
            try:
                conn.close()
            except OSError:
                pass
            return

        if self.mode == "smoothie":
            self._serve_smoothie(conn)
            return

        with self._lock:
            self._active_conn = conn
        conn.settimeout(0.2)
        buf = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                self._consume(conn, buf)
        finally:
            with self._lock:
                if self._active_conn is conn:
                    self._active_conn = None
            try:
                conn.close()
            except OSError:
                pass

    def _serve_smoothie(self, conn):
        """Legacy plaintext line protocol: never framed, so a hello would
        never be understood — and per the controller's own rule, must never
        even be attempted here. Only replies to the protocol detector's
        "echo" probe, enough to make the controller pick Smoothie mode;
        every other byte received is just recorded for the test to inspect.
        """
        conn.settimeout(0.2)
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                with self._lock:
                    self.smoothie_bytes_received.extend(chunk)
                if b"echo" in chunk:
                    self._send(conn, b"echo\r\nok\r\n")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _consume(self, conn, buf):
        while True:
            idx = buf.find(_HEADER)
            if idx < 0:
                if len(buf) > 1:
                    del buf[:-1]
                return
            if idx > 0:
                del buf[:idx]
            if len(buf) < 4:
                return
            length = (buf[2] << 8) | buf[3]
            total = 4 + length + 2
            if len(buf) < total:
                return
            frame = bytes(buf[:total])
            del buf[:total]
            parsed = validate_packet_data(frame[2:-2])
            if parsed is None:
                continue
            with self._lock:
                self.frames_received.append((parsed.ptype, parsed.payload))
            self._handle_frame(conn, parsed.ptype, parsed.payload)

    def _handle_frame(self, conn, ptype, payload):
        if self.mode == "silent":
            return  # answers nothing at all: models a dead/unresponsive link.

        if ptype == PTYPE_CTRL_SINGLE:
            if payload[:1] == b"?":
                self._send(conn, build_frame(PTYPE_STATUS_RES, DEFAULT_STATUS))
            return

        if ptype == PTYPE_HELLO:
            # Recorded regardless of mode: the point of "old" is that the
            # machine received a hello and did nothing with it, not that it
            # never arrived.
            with self._lock:
                self.hellos_received.append(payload)

        if self.mode == "old":
            return  # old firmware: every new type is silently dropped.

        if ptype == PTYPE_HELLO:
            if self.ack_delay:
                time.sleep(self.ack_delay)
            self._send(conn, build_frame(PTYPE_HELLO_ACK, bytes([1, self.ack_result, 0])))
            if self.close_after_ack is not None:
                threading.Thread(target=self._close_after_ack, args=(conn,), daemon=True).start()
            if self.publish_status_enabled and self.ack_result == HELLO_ACCEPTED:
                threading.Thread(target=self._publish_status_loop, args=(conn,), daemon=True).start()
            return

        if ptype == PTYPE_CLIENT_LIST_REQ:
            with self._lock:
                self.client_list_requests += 1
            name = b"Fake Machine Self"
            entry = (0xAAAABBBBCCCCDDDD).to_bytes(8, "big") + bytes([len(name)]) + name + bytes([0, 1])
            self._send(conn, build_frame(PTYPE_CLIENT_LIST_REPLY, bytes([1]) + entry))
            return

    def _close_after_ack(self, conn):
        time.sleep(self.close_after_ack)
        try:
            conn.close()
        except OSError:
            pass

    def _publish_status_loop(self, conn):
        """Stands in for the firmware's own proactive status publish once
        this client is identified: sends an unsolicited PTYPE_STATUS_RES
        on a fixed interval, same shape as the on-demand `?` reply, until
        the connection closes or the machine stops. See publish_status()
        for a one-shot version a test can call directly instead/as well.
        """
        while not self._stop.is_set():
            time.sleep(self.status_interval_s)
            with self._lock:
                still_active = self._active_conn is conn
            if not still_active:
                return
            self._send(conn, build_frame(PTYPE_STATUS_RES, DEFAULT_STATUS))

    def publish_status(self, text=DEFAULT_STATUS):
        """Test helper: send one unsolicited PTYPE_STATUS_RES to the
        currently-connected client right now, as the machine's own publish
        would. Returns False if there is no connected client to send to."""
        with self._lock:
            conn = self._active_conn
        if conn is None:
            return False
        self._send(conn, build_frame(PTYPE_STATUS_RES, text))
        return True

    def send_published_line(self, source_id, source_name, text, more=False):
        """Test helper: publish one PTYPE_PUBLISHED_LINE frame to the
        currently-connected client, as the machine would relay another
        (or this) controller's command/reply. ``source_name``/``text`` are
        bytes. Returns False if there is no connected client to send to."""
        with self._lock:
            conn = self._active_conn
        if conn is None:
            return False
        payload = (
            source_id.to_bytes(8, "big") + bytes([len(source_name)]) + source_name + bytes([1 if more else 0]) + text
        )
        self._send(conn, build_frame(PTYPE_PUBLISHED_LINE, payload))
        return True

    def send_control_changed_event(self, holder_id, holder_name=b""):
        """Test helper: publish one PTYPE_EVENT frame, kind
        EVENT_KIND_CONTROL_CHANGED, as the machine's own control gate would
        (firmware's ``build_control_changed_event``): kind(1) + holder_id(8, BE) +
        holder_name_len(1) + holder_name. ``holder_id == 0`` with an empty
        ``holder_name`` is the machine's own "nobody has control" encoding.
        ``holder_name`` is bytes. Returns False if there is no connected
        client to send to."""
        with self._lock:
            conn = self._active_conn
        if conn is None:
            return False
        payload = (
            bytes([EVENT_KIND_CONTROL_CHANGED]) + holder_id.to_bytes(8, "big") + bytes([len(holder_name)]) + holder_name
        )
        self._send(conn, build_frame(PTYPE_EVENT, payload))
        return True

    def send_relay(self, source_id, payload):
        """Test helper: publish one PTYPE_RELAY frame to the currently-
        connected client, as the machine would repeat another identified
        client's relay (firmware's ``build_relay_frame``): source_id(8, BE)
        + payload verbatim. ``payload`` is bytes. Returns False if there is
        no connected client to send to."""
        with self._lock:
            conn = self._active_conn
        if conn is None:
            return False
        self._send(conn, build_frame(PTYPE_RELAY, source_id.to_bytes(8, "big") + payload))
        return True

    def send_upload_finished_event(self, path, size=0, checksum_type=0, checksum=b""):
        """Test helper: publish one PTYPE_EVENT frame, kind
        EVENT_KIND_UPLOAD_FINISHED (firmware's ``build_upload_finished_event``):
        kind(1) + path_len(1) + path + size(4, BE) + checksum_type(1) +
        checksum. ``path``/``checksum`` are bytes. Returns False if there is
        no connected client to send to."""
        with self._lock:
            conn = self._active_conn
        if conn is None:
            return False
        payload = (
            bytes([EVENT_KIND_UPLOAD_FINISHED, len(path)])
            + path
            + size.to_bytes(4, "big")
            + bytes([checksum_type])
            + checksum
        )
        self._send(conn, build_frame(PTYPE_EVENT, payload))
        return True

    def send_play_started_event(self, path):
        """Test helper: publish one PTYPE_EVENT frame, kind
        EVENT_KIND_PLAY_STARTED (firmware's ``build_play_started_event``):
        kind(1) + path_len(1) + path. ``path`` is bytes. Returns False if
        there is no connected client to send to."""
        with self._lock:
            conn = self._active_conn
        if conn is None:
            return False
        payload = bytes([EVENT_KIND_PLAY_STARTED, len(path)]) + path
        self._send(conn, build_frame(PTYPE_EVENT, payload))
        return True

    def send_normal_info(self, text):
        """Test helper: send ``text`` (bytes, normally ending in ``\\r\\n``)
        as a PTYPE_NORMAL_INFO frame — the type every ordinary command
        reply (an "ok", an error such as the control gate's refusal, ls/cat
        output, ...) is actually sent as on real firmware (see
        WifiProvider::printf()/PacketMessage()), as opposed to
        PTYPE_STATUS_RES, which only ``?``/diagnose replies and the
        proactive status publish use. Returns False if there is no
        connected client to send to."""
        with self._lock:
            conn = self._active_conn
        if conn is None:
            return False
        self._send(conn, build_frame(PTYPE_NORMAL_INFO, text))
        return True

    def _send(self, conn, frame):
        with self._lock:
            try:
                conn.sendall(frame)
            except OSError:
                pass

    # -- assertion helpers -----------------------------------------------

    def frames_of_type(self, ptype):
        with self._lock:
            return [payload for (t, payload) in self.frames_received if t == ptype]

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()
