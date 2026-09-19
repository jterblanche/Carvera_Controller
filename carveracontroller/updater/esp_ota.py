"""HTTP OTA upload of a raw ESP32 image to a Makera Z1."""

from __future__ import annotations

import http.client
import logging
import socket
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

OTA_PATH = "/update"
OTA_PORT = 80
OTA_TIMEOUT_S = 180
OTA_BOUNDARY = "z1-firmware-upload"
OTA_FILENAME = "mainboard.bin"
OTA_FIELD_NAME = "firmware"

ProgressFn = Callable[[int, int], None]


class EspOtaError(Exception):
    """Raised when an ESP OTA upload cannot be completed."""


class EspOtaCancelled(EspOtaError):
    """Raised when the user cancels an ESP OTA upload."""


def wifi_http_host(connection_address: str | None) -> str:
    """Return the machine hostname/IP from a controller WiFi address."""
    text = (connection_address or "").strip()
    if not text:
        return ""
    return text.split(":")[0].strip()


def multipart_prefix(boundary: str = OTA_BOUNDARY, filename: str = OTA_FILENAME) -> bytes:
    """Return the multipart headers used by the Z1 ESP HTTP updater."""
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{OTA_FIELD_NAME}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
    ).encode("ascii")


def upload_esp_ota(
    host: str,
    image_path: str | Path,
    *,
    timeout: float = OTA_TIMEOUT_S,
    progress: ProgressFn | None = None,
    cancel_event: threading.Event | None = None,
    on_connection: Callable[[object], None] | None = None,
    connection_cls=http.client.HTTPConnection,
    port: int = OTA_PORT,
    path: str = OTA_PATH,
    boundary: str = OTA_BOUNDARY,
    filename: str = OTA_FILENAME,
) -> None:
    """POST *image_path* to ``http://{host}/update`` as multipart form-data.

    Matches the Z1 ESP HTTP updater: HTTP/1.1, no ``Expect: 100-continue``,
    field name ``firmware``, filename ``mainboard.bin``.
    """
    target = (host or "").strip()
    if not target:
        raise EspOtaError("ESP firmware updates require a WiFi connection.")
    source = Path(image_path)
    try:
        file_size = source.stat().st_size
    except OSError as exc:
        raise EspOtaError("Couldn't find the ESP firmware file.") from exc
    if file_size <= 0:
        raise EspOtaError("The ESP firmware file is empty.")

    prefix = multipart_prefix(boundary, filename)
    total = len(prefix) + file_size
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(total),
        "Connection": "close",
    }
    logger.info("ESP OTA: POST http://%s:%s%s (%s bytes)", target, port, path, file_size)
    body = _ProgressBody(source, prefix, total, progress, cancel_event)
    conn = connection_cls(target, port, timeout=timeout)
    if on_connection is not None:
        on_connection(conn)
    sent_complete = False
    status = 0
    try:
        conn.request("POST", path, body=body, headers=headers)
        sent_complete = body.sent >= total
        response = conn.getresponse()
        status = response.status
        response.read()
    except EspOtaCancelled:
        raise
    except (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected) as exc:
        if sent_complete:
            logger.info("ESP OTA: connection closed after upload. This eis expected, treating as success")
            return
        raise EspOtaError("Couldn't send the ESP firmware. Check the WiFi connection and try again.") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise EspOtaError("The ESP firmware update timed out.") from exc
    except OSError as exc:
        raise EspOtaError("Couldn't send the ESP firmware. Check the WiFi connection and try again.") from exc
    finally:
        body.close()
        try:
            conn.close()
        except OSError:
            pass

    if status < 200 or status >= 300:
        raise EspOtaError("The machine rejected the ESP firmware update.")
    logger.info("ESP OTA: transfer succeeded (HTTP %s)", status)


class _ProgressBody:
    """File-like multipart body that reports progress and honors cancel."""

    def __init__(
        self,
        path: Path,
        prefix: bytes,
        total: int,
        progress: ProgressFn | None,
        cancel_event: threading.Event | None,
    ):
        self._handle = path.open("rb")
        self._prefix = prefix
        self._prefix_offset = 0
        self._total = total
        self._progress = progress
        self._cancel_event = cancel_event
        self.sent = 0

    def read(self, size: int = -1) -> bytes:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise EspOtaCancelled("ESP firmware update cancelled.")
        if size == 0:
            return b""
        chunks: list[bytes] = []
        remaining = size
        if self._prefix_offset < len(self._prefix):
            if remaining < 0:
                prefix_chunk = self._prefix[self._prefix_offset :]
            else:
                prefix_chunk = self._prefix[self._prefix_offset : self._prefix_offset + remaining]
            self._prefix_offset += len(prefix_chunk)
            chunks.append(prefix_chunk)
            if remaining >= 0:
                remaining -= len(prefix_chunk)
        if remaining != 0:
            file_chunk = self._handle.read() if remaining < 0 else self._handle.read(remaining)
            if file_chunk:
                chunks.append(file_chunk)
        data = b"".join(chunks)
        if data:
            self.sent += len(data)
            if self._progress is not None:
                self._progress(self.sent, self._total)
        return data

    def close(self) -> None:
        self._handle.close()
