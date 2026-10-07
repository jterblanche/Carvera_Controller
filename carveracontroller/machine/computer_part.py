"""The computer part of the controller's identity, and the launch part.

The id a controller sends in its hello is worked out afresh at every start,
never kept in the settings file, so a copied settings file cannot carry it
and a settings reset or reinstall cannot lose it. It is a keyed scramble
(the first 8 bytes of HMAC-SHA256 with a key only this app uses) of:

- the operating system's install identifier: Windows ``MachineGuid``, Linux
  ``/etc/machine-id``, the macOS hardware UUID, Android ``ANDROID_ID`` or
  the iOS ``identifierForVendor``;
- the OS user, on desktops (the account SID on Windows, the uid elsewhere);
- a copy number: the first of ``copy-<base>-1.lock``, ``copy-<base>-2.lock``
  and so on that this process can lock. The operating system drops the lock
  when the process ends, however it ends, so the first copy started is
  always copy 1. Mobile platforms run one copy, always copy 1.

Where no usable identifier exists (empty, missing, all zeros, or a value
known to be shared, such as the empty ``/etc/machine-id`` of a container),
a random value is used instead, kept in a small file of its own outside the
settings file. That file is named for a fingerprint of what could be read
plus the host name, so a copy of it on another computer is not used there.

"Make this a separate controller" adds one more random value, kept the same
way, for a computer that is a clone of another.

Nothing raw is ever sent: only the 8-byte scramble goes on the network.
Every step that can fail falls back to a value that is still unique while
this process runs, even if it is not stable across restarts.

The launch part is chosen once per start: 4 bytes of start time in seconds,
then 4 random bytes. The machine uses it to tell this process reconnecting
from a new start of the same computer part.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import importlib
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# The scramble's key. Fixed and public: its purpose is that the original
# identifier cannot be recovered from what is sent, and that the result does
# not match what another app derives from the same identifier.
APP_KEY = b"carvera-controller computer part v1"

# An environment variable, read once at start, that names the whole computer
# part instead of the operating system. For test rigs that need each
# container to be its own controller, or two to be the same one.
OVERRIDE_ENV = "CARVERA_CONTROLLER_SEAT"

DESKTOP_PLATFORMS = frozenset({"windows", "macos", "linux"})

# More copies than anyone runs; past this the copy number is a random value.
MAX_COPIES = 32

LINUX_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")
IOREG = "/usr/sbin/ioreg"

# Values reported by many devices at once. The first is the SMBIOS UUID of
# boards whose maker never set one; the second is the ANDROID_ID a batch of
# early Android devices all shared.
KNOWN_SHARED_IDENTIFIERS = frozenset({"03000200040005000006000700080009", "9774d56d682e549c"})

_SECRET_PATTERN = re.compile(r"[0-9a-f]{32}")
_IOREG_UUID = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')

# Lock files held by this process; kept here so they stay open (and locked)
# for as long as the process runs.
_held_locks: list[int] = []


@dataclass(frozen=True)
class OsIdentifier:
    """What the operating system offered. ``value`` is None when it could
    not be read at all; ``user`` is "" where the platform has one user per
    app."""

    platform: str
    value: str | None
    user: str


@dataclass(frozen=True)
class ComputerPart:
    """The derived computer part. ``source`` says where it came from: "os",
    "fallback" (the stored random value), "random" (nothing could be stored,
    so unique only while this process runs) or "override". ``copy`` is None
    when no copy slot could be locked (a random value stands in for it)."""

    id: int
    source: str
    copy: int | None
    state_dir: Path | None = field(repr=False, default=None)
    _base: str = field(repr=False, default="")
    _inputs: tuple[str, ...] = field(repr=False, default=())
    _lock_fd: int | None = field(repr=False, default=None, compare=False)

    def made_separate(self) -> ComputerPart:
        """Add a new random value to this computer part and keep it, so this
        controller is told apart from a clone of this computer from now on.
        Keeps the copy slot. Raises OSError if the value cannot be stored:
        a separation that does not last would only move the clash to the
        next start."""
        if self.state_dir is None:
            raise OSError("no folder to keep the value in")
        separation = secrets.token_hex(16)
        _ensure_dir(self.state_dir)
        _write_secret(self.state_dir / f"separate-{self._base}", separation)
        if _read_secret(self.state_dir / f"separate-{self._base}") != separation:
            raise OSError("the value could not be read back")
        return replace(self, id=_scramble("computer", *self._inputs, separation))

    def release(self) -> None:
        """Give up the copy slot, as the end of the process would."""
        if self._lock_fd is not None and self._lock_fd in _held_locks:
            _held_locks.remove(self._lock_fd)
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)


def release_all_for_tests() -> None:
    """Close every copy slot this process holds."""
    while _held_locks:
        fd = _held_locks.pop()
        with contextlib.suppress(OSError):
            os.close(fd)


# ---------------------------------------------------------------------------
# Platform and identifier
# ---------------------------------------------------------------------------


def current_platform(env: Mapping[str, str] | None = None, sys_platform: str | None = None) -> str:
    """One of "android", "ios", "windows", "macos" or "linux" (also used for
    any other Unix, which may have ``/etc/machine-id`` too)."""
    env = os.environ if env is None else env
    sys_platform = sys.platform if sys_platform is None else sys_platform
    if any(key in env for key in ("ANDROID_ARGUMENT", "ANDROID_PRIVATE", "ANDROID_APP_PATH")):
        return "android"
    if env.get("KIVY_BUILD") == "ios" or sys_platform == "ios":
        return "ios"
    if sys_platform in ("win32", "cygwin"):
        return "windows"
    if sys_platform == "darwin":
        return "macos"
    return "linux"


def read_os_identifier(
    platform: str,
    *,
    linux_paths: tuple[str, ...] = LINUX_MACHINE_ID_PATHS,
    uid: int | None = None,
) -> OsIdentifier:
    """Read the platform's install identifier and user part, as an ordinary
    user. Never raises: what cannot be read is None (identifier) or a
    best-effort value (user)."""
    if platform == "windows":
        return OsIdentifier(platform, _quietly(_windows_machine_guid), _windows_user())
    if platform == "android":
        return OsIdentifier(platform, _quietly(_android_id), "")
    if platform == "ios":
        return OsIdentifier(platform, _quietly(_ios_identifier_for_vendor), "")
    user = str(_posix_uid() if uid is None else uid)
    if platform == "macos":
        return OsIdentifier(platform, _quietly(_macos_platform_uuid), user)
    return OsIdentifier(platform, _linux_machine_id(linux_paths), user)


def usable_identifier(value: str | None) -> bool:
    """False for an identifier that does not tell computers apart: missing,
    blank, too short, all zeros, all F, "uninitialized" (systemd's marker
    for an image not yet booted), or one known to be shared."""
    if value is None:
        return False
    normal = _normalise(value)
    if len(normal) < 8 or normal == "uninitialized":
        return False
    if set(normal) <= {"0"} or set(normal) <= {"f"}:
        return False
    return normal not in KNOWN_SHARED_IDENTIFIERS


def _normalise(value: str) -> str:
    return value.strip().lower().replace("-", "").replace("{", "").replace("}", "")


def _quietly(read: Any) -> str | None:
    try:
        value = read()
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _linux_machine_id(paths: tuple[str, ...]) -> str | None:
    """The first usable of the given files; else the first one that exists,
    as read (empty in a container), so the fallback can fingerprint it."""
    first_read: str | None = None
    for path in paths:
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                value = f.read().strip()
        except OSError:
            continue
        if usable_identifier(value):
            return value
        if first_read is None:
            first_read = value
    return first_read


def _windows_machine_guid() -> str:
    winreg: Any = importlib.import_module("winreg")
    key = winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    )
    try:
        value, _kind = winreg.QueryValueEx(key, "MachineGuid")
    finally:
        winreg.CloseKey(key)
    return str(value)


def _windows_user() -> str:
    try:
        return _windows_user_sid()
    except Exception:
        domain = os.environ.get("USERDOMAIN", "")
        name = os.environ.get("USERNAME", "")
        return f"{domain}\\{name}"


def _windows_user_sid() -> str:  # pragma: no cover - needs Windows
    """The account SID of this process's user, through advapi32."""
    import ctypes
    from ctypes import wintypes

    ctypes_any: Any = ctypes
    advapi32 = ctypes_any.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes_any.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    token_query = 0x0008
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)):
        raise OSError("OpenProcessToken failed")
    try:
        needed = wintypes.DWORD()
        token_user = 1
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, token_user, buffer, needed, ctypes.byref(needed)):
            raise OSError("GetTokenInformation failed")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError("ConvertSidToStringSidW failed")
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def _posix_uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else 0


def _macos_platform_uuid() -> str | None:
    result = subprocess.run(
        [IOREG, "-rd1", "-c", "IOPlatformExpertDevice"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    match = _IOREG_UUID.search(result.stdout or "")
    return match.group(1) if match else None


def _android_activity() -> Any:
    jnius: Any = importlib.import_module("jnius")
    return jnius.autoclass("org.kivy.android.PythonActivity").mActivity


def _android_id() -> str | None:
    jnius: Any = importlib.import_module("jnius")
    secure = jnius.autoclass("android.provider.Settings$Secure")
    value = secure.getString(_android_activity().getContentResolver(), secure.ANDROID_ID)
    return None if value is None else str(value)


def _ios_identifier_for_vendor() -> str | None:
    pyobjus: Any = importlib.import_module("pyobjus")
    vendor_id = pyobjus.autoclass("UIDevice").currentDevice().identifierForVendor
    if vendor_id is None:
        return None
    text = vendor_id.UUIDString()
    return str(text.UTF8String() if hasattr(text, "UTF8String") else text)


# ---------------------------------------------------------------------------
# Where the small files live
# ---------------------------------------------------------------------------


def default_state_dir(platform: str, env: Mapping[str, str] | None = None, home: Path | None = None) -> Path | None:
    """A per-user folder outside the settings, so neither a settings reset
    nor a different settings folder (a portable copy) changes the identity.
    On Android, the app's no-backup folder, which a phone set up from a
    backup does not inherit. None if no folder can be named."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if platform == "windows":
        local = env.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / "CarveraController"
    if platform in ("macos", "ios"):
        return home / "Library" / "Application Support" / "CarveraController"
    if platform == "android":
        try:
            return Path(str(_android_activity().getNoBackupFilesDir().getAbsolutePath())) / "carvera-controller"
        except Exception:
            return None
    state = env.get("XDG_STATE_HOME")
    return (Path(state) if state else home / ".local" / "state") / "carvera-controller"


# ---------------------------------------------------------------------------
# Deriving
# ---------------------------------------------------------------------------


def derive_computer_part(
    platform: str,
    state_dir: Path | None,
    identifier: OsIdentifier,
    *,
    hostname: str = "",
    override: str | None = None,
) -> ComputerPart:
    """The computer part for this process. Takes a copy slot on desktops and
    holds it until :meth:`ComputerPart.release` or the end of the process."""
    if override:
        base_inputs: tuple[str, ...] = ("override", override)
        base = _scramble_hex("base", *base_inputs)
        separation = _read_secret(state_dir / f"separate-{base}") if state_dir is not None else None
        return ComputerPart(
            id=_scramble("computer", *base_inputs, separation or ""),
            source="override",
            copy=None,
            state_dir=state_dir,
            _base=base,
            _inputs=base_inputs,
        )

    if usable_identifier(identifier.value):
        assert identifier.value is not None
        source, secret = "os", _normalise(identifier.value)
    else:
        source, secret = _fallback_secret(platform, state_dir, identifier, hostname)

    base_inputs = (platform, source, secret, identifier.user)
    base = _scramble_hex("base", *base_inputs)

    copy: int | None = 1
    lock_fd: int | None = None
    copy_token = "1"
    if platform in DESKTOP_PLATFORMS:
        copy, lock_fd = _take_copy_slot(state_dir, base)
        copy_token = str(copy) if copy is not None else "random:" + secrets.token_hex(16)

    inputs = (*base_inputs, copy_token)
    separation = _read_secret(state_dir / f"separate-{base}") if state_dir is not None else None
    return ComputerPart(
        id=_scramble("computer", *inputs, separation or ""),
        source=source,
        copy=copy,
        state_dir=state_dir,
        _base=base,
        _inputs=inputs,
        _lock_fd=lock_fd,
    )


def computer_part_for_this_process(state_dir: Path | None = None) -> ComputerPart:
    """The computer part from this computer's own identifier, user and copy
    slot, or from the test-rig override. Never raises: whatever fails, the
    result is still unique while this process runs."""
    override = os.environ.get(OVERRIDE_ENV, "").strip() or None
    try:
        platform = current_platform()
        if state_dir is None:
            state_dir = default_state_dir(platform)
        if override:
            return derive_computer_part(platform, state_dir, OsIdentifier(platform, None, ""), override=override)
        identifier = read_os_identifier(platform)
        return derive_computer_part(platform, state_dir, identifier, hostname=_hostname())
    except Exception:
        return ComputerPart(id=_scramble("random", secrets.token_hex(16)), source="random", copy=None)


def new_launch_part(now: float | None = None) -> int:
    """4 bytes of start time in seconds, then 4 random bytes. The time makes
    two starts differ even if the random source were poor; the random half
    makes two starts in one second differ."""
    seconds = int(time.time() if now is None else now) & 0xFFFFFFFF
    return (seconds << 32) | secrets.randbits(32)


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _scramble_bytes(*parts: str) -> bytes:
    message = b"".join(len(p.encode()).to_bytes(4, "big") + p.encode() for p in parts)
    return hmac.new(APP_KEY, message, hashlib.sha256).digest()


def _scramble(*parts: str) -> int:
    """The first 8 bytes, big-endian. Zero means "nobody" on the wire, so a
    zero result becomes one."""
    return int.from_bytes(_scramble_bytes(*parts)[:8], "big") or 1


def _scramble_hex(*parts: str) -> str:
    return _scramble_bytes(*parts)[:8].hex()


# ---------------------------------------------------------------------------
# Fallback value and separation value
# ---------------------------------------------------------------------------


def _fallback_secret(platform: str, state_dir: Path | None, identifier: OsIdentifier, hostname: str) -> tuple[str, str]:
    """("fallback", the stored random value for this fingerprint), or
    ("random", a value for this process only) when nothing can be stored."""
    fingerprint = _scramble_hex("fallback", platform, identifier.value or "", identifier.user, hostname)
    if state_dir is not None:
        try:
            _ensure_dir(state_dir)
            stored = _read_or_create_secret(state_dir / f"fallback-{fingerprint}")
        except OSError:
            stored = None
        if stored is not None:
            return "fallback", stored
    return "random", secrets.token_hex(16)


def _ensure_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _read_secret(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    return text if _SECRET_PATTERN.fullmatch(text) else None


def _write_secret(path: Path, value: str) -> None:
    """Write through a temporary file and rename it into place, so a reader
    never sees half a value."""
    temp = _temp_beside(path)
    try:
        _write_new_file(temp, value)
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def _read_or_create_secret(path: Path) -> str | None:
    """The value in ``path``, creating it first if it is missing or not a
    valid value. When two processes create it at once, both end up with the
    one that got there first. None if it cannot be read back."""
    existing = _read_secret(path)
    if existing is not None:
        return existing
    temp = _temp_beside(path)
    try:
        _write_new_file(temp, secrets.token_hex(16))
        try:
            # Link, not rename: fails if another process created it first.
            os.link(temp, path)
        except FileExistsError:
            if _read_secret(path) is None:
                os.replace(temp, path)  # it exists but holds no valid value
        except OSError:
            # A file system without hard links.
            if not path.exists():
                os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
    return _read_secret(path)


def _temp_beside(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")


def _write_new_file(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (value + "\n").encode("ascii"))
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Copy slot
# ---------------------------------------------------------------------------


def _take_copy_slot(state_dir: Path | None, base: str) -> tuple[int | None, int | None]:
    """(copy number, open lock file) for the first free slot, or (None,
    None) if no slot can be locked at all, for any reason."""
    if state_dir is None:
        return None, None
    try:
        _ensure_dir(state_dir)
    except OSError:
        return None, None
    for number in range(1, MAX_COPIES + 1):
        try:
            fd = os.open(state_dir / f"copy-{base}-{number}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None, None
        if _lock(fd):
            _held_locks.append(fd)
            return number, fd
        os.close(fd)
    return None, None


def _lock(fd: int) -> bool:
    """Lock the file without waiting. False if another process holds it, or
    it cannot be locked."""
    try:
        if os.name == "nt":  # pragma: no cover - needs Windows
            msvcrt: Any = importlib.import_module("msvcrt")
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl: Any = importlib.import_module("fcntl")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True
