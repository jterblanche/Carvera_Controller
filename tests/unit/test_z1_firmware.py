"""Classify Makera Z1 combined bundles vs LPC-only firmware images."""

from __future__ import annotations

import zlib
from pathlib import Path

from carveracontroller.updater.z1_firmware import (
    FLAG_ESP,
    FLAG_LPC,
    HEADER,
    KIND_BUNDLE,
    KIND_CARVERA,
    KIND_ESP,
    KIND_LPC,
    MAKERA_MAGIC,
    classify_z1_firmware_path,
    firmware_install_plan,
    is_z1_combined_bundle,
    is_z1_esp_image,
)


def _bundle(
    esp: bytes = b"ESP!",
    lpc: bytes = b"LPCIMAGE",
    *,
    magic: int = MAKERA_MAGIC,
    header_version: int = 1,
    header_length: int | None = None,
    flags: int | None = None,
    header_crc: int | None = None,
) -> bytes:
    if flags is None:
        flags = (FLAG_ESP if esp else 0) | (FLAG_LPC if lpc else 0)
    length = HEADER.size if header_length is None else header_length
    values = (magic, header_version, length, flags, 0, len(esp), len(lpc), 0, 0)
    header = HEADER.pack(*values, 0, 0)
    crc = zlib.crc32(header[:24]) & 0xFFFFFFFF if header_crc is None else header_crc
    file_crc = zlib.crc32(header[:28] + esp + lpc) & 0xFFFFFFFF
    return HEADER.pack(*values, crc, file_crc) + esp + lpc


def _esp_image() -> bytes:
    # First bytes of a Makera Z1 mainboard.bin (ESP32 image magic 0xE9, 6 segments).
    return bytes.fromhex("e906023fe4583740ee00000009000000") + b"\x00" * 32


def test_combined_bundle_requires_makera_header_and_both_images():
    combined = _bundle()
    assert is_z1_combined_bundle(combined)
    assert is_z1_combined_bundle(_bundle(header_version=2))
    assert not is_z1_combined_bundle(b"")
    assert not is_z1_combined_bundle(b"\x00" * 256)
    assert not is_z1_combined_bundle(_bundle(magic=0xDEADBEEF))
    assert not is_z1_combined_bundle(_bundle(esp=b"", lpc=b"LPCIMAGE"))
    assert not is_z1_combined_bundle(_bundle(esp=b"ESP!", lpc=b""))
    assert not is_z1_combined_bundle(_bundle() + b"extra")
    assert not is_z1_combined_bundle(_bundle(header_version=3))
    assert not is_z1_combined_bundle(_bundle(header_length=16))
    assert not is_z1_combined_bundle(_bundle(flags=FLAG_LPC))
    assert not is_z1_combined_bundle(_bundle(header_crc=0))


def test_firmware_install_plan_routes_images(tmp_path: Path):
    bundle = tmp_path / "combined.bin"
    bundle.write_bytes(_bundle())
    lpc_only = tmp_path / "lpc.bin"
    lpc_only.write_bytes(b"\x00" * 64)
    esp = tmp_path / "mainboard.bin"
    esp.write_bytes(_esp_image())

    combined = firmware_install_plan("Z1", bundle)
    assert not combined.use_ota
    assert combined.kind == KIND_BUNDLE
    assert combined.kind_label == "combined LPC+ESP bundle"
    assert combined.remote_path == "/sd/firmware.bin"

    lpc = firmware_install_plan("Z1", lpc_only)
    assert lpc.kind == KIND_LPC
    assert lpc.remote_path == "/sd/lpc1768.bin"

    ota = firmware_install_plan("Z1", esp, wifi_address="192.168.9.20:2222")
    assert ota.use_ota
    assert ota.kind == KIND_ESP
    assert ota.kind_label == "ESP-only (mainboard)"
    assert ota.remote_path == ""
    assert ota.ota_host == "192.168.9.20"
    assert ota.method_description == "WiFi OTA to http://192.168.9.20/update"

    assert firmware_install_plan("", esp, wifi_address="192.168.9.20:2222").use_ota
    assert firmware_install_plan("C1", bundle).remote_path == "/sd/firmware.bin"
    assert firmware_install_plan("C1", lpc_only).kind == KIND_CARVERA
    assert firmware_install_plan("C1", lpc_only).remote_path == "/sd/firmware.bin"
    assert firmware_install_plan("CA1").remote_path == "/sd/firmware.bin"
    assert firmware_install_plan("").remote_path == "/sd/firmware.bin"
    assert firmware_install_plan("Z1").remote_path == "/sd/lpc1768.bin"
    assert firmware_install_plan("Z1Pro").remote_path == "/sd/lpc1768.bin"


def test_esp_image_is_detected_and_not_a_bundle(tmp_path: Path):
    image = _esp_image()
    assert is_z1_esp_image(image)
    assert not is_z1_combined_bundle(image)
    assert not is_z1_esp_image(b"")
    assert not is_z1_esp_image(b"\x00" * 64)
    assert not is_z1_esp_image(_bundle())
    path = tmp_path / "MakeraZ1-1.1.2.0113-mainboard.bin"
    path.write_bytes(image)
    lpc_only = tmp_path / "controller.bin"
    lpc_only.write_bytes(bytes.fromhex("00800010") + b"\x00" * 60)
    missing = tmp_path / "missing.bin"
    combined = tmp_path / "combined.bin"
    combined.write_bytes(_bundle())
    assert classify_z1_firmware_path(path) == KIND_ESP
    assert classify_z1_firmware_path(lpc_only) == KIND_LPC
    assert classify_z1_firmware_path(combined) == KIND_BUNDLE
    assert classify_z1_firmware_path(missing) == KIND_LPC
    assert classify_z1_firmware_path(None) == KIND_LPC
