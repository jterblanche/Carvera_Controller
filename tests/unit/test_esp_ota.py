"""ESP OTA multipart upload used for Z1 mainboard firmware."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from carveracontroller.updater.esp_ota import (
    EspOtaCancelled,
    EspOtaError,
    multipart_prefix,
    upload_esp_ota,
    wifi_http_host,
)


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"OK"):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeConnection:
    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.method = None
        self.url = None
        self.body = None
        self.headers = None
        self.closed = False
        self.response = _FakeResponse()
        self.error = None

    def request(self, method, url, body=None, headers=None):
        if self.error is not None:
            raise self.error
        self.method = method
        self.url = url
        self.headers = dict(headers or {})
        self.body = body.read() if hasattr(body, "read") else body

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_wifi_http_host_strips_cnc_port():
    assert wifi_http_host("192.168.9.20:2222") == "192.168.9.20"
    assert wifi_http_host("192.168.9.20") == "192.168.9.20"
    assert wifi_http_host("") == ""
    assert wifi_http_host(None) == ""


def test_upload_esp_ota_posts_multipart_body(tmp_path: Path):
    image = b"esp-firmware-bytes"
    path = tmp_path / "mainboard.bin"
    path.write_bytes(image)
    created = []

    def factory(host, port, timeout):
        conn = _FakeConnection(host, port, timeout)
        created.append(conn)
        return conn

    progress = []
    upload_esp_ota(
        "192.168.9.20",
        path,
        connection_cls=factory,
        progress=lambda sent, total: progress.append((sent, total)),
    )
    conn = created[0]
    assert conn.method == "POST"
    assert conn.url == "/update"
    assert conn.port == 80
    assert conn.timeout == 180
    assert conn.headers["Content-Type"] == "multipart/form-data; boundary=z1-firmware-upload"
    assert "Expect" not in conn.headers
    prefix = multipart_prefix()
    assert conn.body == prefix + image
    assert conn.headers["Content-Length"] == str(len(conn.body))
    assert conn.body.startswith(b"--z1-firmware-upload\r\n")
    assert b'name="firmware"' in conn.body
    assert b'filename="mainboard.bin"' in conn.body
    assert conn.closed
    assert progress
    assert progress[-1] == (len(conn.body), len(conn.body))


def test_upload_esp_ota_requires_host_and_rejects_http_errors(tmp_path: Path):
    path = tmp_path / "mainboard.bin"
    path.write_bytes(b"esp")
    with pytest.raises(EspOtaError, match="WiFi"):
        upload_esp_ota("", path, connection_cls=_FakeConnection)
    created = []

    def factory(host, port, timeout):
        conn = _FakeConnection(host, port, timeout)
        conn.response = _FakeResponse(status=500)
        created.append(conn)
        return conn

    with pytest.raises(EspOtaError, match="rejected"):
        upload_esp_ota("192.168.9.20", path, connection_cls=factory)


def test_upload_esp_ota_cancel(tmp_path: Path):
    path = tmp_path / "mainboard.bin"
    path.write_bytes(b"esp-firmware")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(EspOtaCancelled):
        upload_esp_ota("192.168.9.20", path, cancel_event=cancel, connection_cls=_FakeConnection)


def test_upload_esp_ota_treats_reset_after_complete_as_success(tmp_path: Path):
    path = tmp_path / "mainboard.bin"
    path.write_bytes(b"esp-firmware")

    class _ResetAfterSend(_FakeConnection):
        def request(self, method, url, body=None, headers=None):
            super().request(method, url, body=body, headers=headers)

        def getresponse(self):
            raise ConnectionResetError("ESP rebooted")

    created = []

    def factory(host, port, timeout):
        conn = _ResetAfterSend(host, port, timeout)
        created.append(conn)
        return conn

    upload_esp_ota("192.168.9.20", path, connection_cls=factory)
    assert created[0].closed
