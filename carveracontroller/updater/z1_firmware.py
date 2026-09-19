"""Classify Makera Z1 firmware images and choose how to install them.

Official Z1 updates come in three shapes:

- Combined LPC+ESP bundles with a Makera header (see
  Carvera_Community_Firmware/build/repack-z1-firmware.py)
- Bare ESP32 images (``*-mainboard.bin``), sent OTA to the ESP HTTP updater
- Bare LPC1768 images (community GitHub builds, ``*-controller.bin``, SWD)

C1/CA1 always install as ``/sd/firmware.bin``.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from .esp_ota import OTA_PATH, wifi_http_host

MAKERA_MAGIC = 0x4D5173EE
HEADER = struct.Struct("<IBBBBIIIIII")
FLAG_ESP = 1
FLAG_LPC = 2
ESP_IMAGE_MAGIC = 0xE9
ESP_MIN_SEGMENTS = 1
ESP_MAX_SEGMENTS = 16

KIND_BUNDLE = "bundle"
KIND_ESP = "esp"
KIND_LPC = "lpc"
KIND_CARVERA = "carvera"

SD_FIRMWARE_BIN = "/sd/firmware.bin"
SD_LPC1768_BIN = "/sd/lpc1768.bin"

_KIND_LABELS = {
    KIND_BUNDLE: "combined LPC+ESP bundle",
    KIND_ESP: "ESP-only (mainboard)",
    KIND_LPC: "LPC-only",
    KIND_CARVERA: "Carvera LPC firmware",
}


@dataclass(frozen=True)
class FirmwareInstallPlan:
    """How a firmware file should be sent to the connected machine."""

    kind: str
    use_ota: bool
    remote_path: str
    ota_host: str = ""

    @property
    def kind_label(self) -> str:
        return _KIND_LABELS.get(self.kind, self.kind)

    @property
    def method_description(self) -> str:
        if self.use_ota:
            dest = "http://%s%s" % (self.ota_host or "<machine-ip>", OTA_PATH)
            return "WiFi OTA to %s" % dest
        return "SD upload as %s" % self.remote_path


def classify_z1_firmware_path(path: str | Path | None) -> str:
    """Return ``bundle``, ``esp``, or ``lpc`` for a local Z1 firmware file."""
    if not path:
        return KIND_LPC
    header, file_size = _read_prefix(path, HEADER.size)
    if header is None or file_size is None:
        return KIND_LPC
    if _header_describes_combined_bundle(header, file_size=file_size):
        return KIND_BUNDLE
    if _prefix_is_esp_image(header):
        return KIND_ESP
    return KIND_LPC


def firmware_install_plan(
    machine_model: str | None = "",
    firmware_path: str | Path | None = None,
    *,
    wifi_address: str | None = "",
) -> FirmwareInstallPlan:
    """Return the install method and destination for a firmware file.

    Combined bundles and ESP images are identified from the file itself. Bare
    LPC images use ``lpc1768.bin`` on Z1 and ``firmware.bin`` on C1/CA1.
    """
    kind = classify_z1_firmware_path(firmware_path)
    if kind == KIND_ESP:
        return FirmwareInstallPlan(kind, True, "", wifi_http_host(wifi_address))
    if kind == KIND_BUNDLE:
        return FirmwareInstallPlan(kind, False, SD_FIRMWARE_BIN)
    if (machine_model or "").startswith("Z1"):
        return FirmwareInstallPlan(KIND_LPC, False, SD_LPC1768_BIN)
    return FirmwareInstallPlan(KIND_CARVERA, False, SD_FIRMWARE_BIN)


def is_z1_combined_bundle(data: bytes) -> bool:
    """Return True if *data* is a combined LPC+ESP Makera Z1 firmware bundle."""
    return _header_describes_combined_bundle(data[: HEADER.size], file_size=len(data))


def is_z1_esp_image(data: bytes) -> bool:
    """Return True if *data* looks like a raw ESP32 firmware image."""
    return _prefix_is_esp_image(data)


def _read_prefix(path: str | Path, size: int) -> tuple[bytes | None, int | None]:
    try:
        file_path = Path(path)
        file_size = file_path.stat().st_size
        with file_path.open("rb") as handle:
            header = handle.read(size)
    except OSError:
        return None, None
    return header, file_size


def _prefix_is_esp_image(data: bytes) -> bool:
    if len(data) < 2 or data[0] != ESP_IMAGE_MAGIC:
        return False
    return ESP_MIN_SEGMENTS <= data[1] <= ESP_MAX_SEGMENTS


def _header_describes_combined_bundle(header: bytes, *, file_size: int) -> bool:
    if len(header) < HEADER.size:
        return False
    fields = HEADER.unpack_from(header)
    magic, header_version, header_length, flags, _reserved = fields[:5]
    esp_size, lpc_size, _esp_version, _lpc_version, _header_crc, _file_crc = fields[5:]
    if magic != MAKERA_MAGIC:
        return False
    if header_version not in (1, 2):
        return False
    if header_length != HEADER.size:
        return False
    esp_present = esp_size != 0
    lpc_present = lpc_size != 0
    if bool(flags & FLAG_ESP) != esp_present or bool(flags & FLAG_LPC) != lpc_present:
        return False
    if not esp_present or not lpc_present:
        return False
    if zlib.crc32(header[:24]) & 0xFFFFFFFF != _header_crc:
        return False
    return file_size == header_length + esp_size + lpc_size
