"""The computer part of the controller's identity, and the launch part.

The computer part is worked out at every start from the operating system's
install identifier, the OS user and a copy number, scrambled with this app's
own key. Nothing about it is kept in the settings file. These tests cover
each platform's way of reading the identifier (with the OS calls mocked),
and the cases that used to give two controllers one id: a copied settings
file, two copies on one computer, a cloned disk image.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from carveracontroller.machine import computer_part as cp
from carveracontroller.machine.computer_part import (
    OsIdentifier,
    current_platform,
    default_state_dir,
    derive_computer_part,
    new_launch_part,
    read_os_identifier,
    usable_identifier,
)

MACHINE_ID = "4c4c4544004d4a10804bb4c04f4a4d32"
OTHER_MACHINE_ID = "9f2b0c7e1d6a4e3f8b5c2a1d0e9f8a7b"


@pytest.fixture(autouse=True)
def _release_held_slots():
    yield
    cp.release_all_for_tests()


def linux_id(value=MACHINE_ID, user="1000"):
    return OsIdentifier(platform="linux", value=value, user=user)


# ---------------------------------------------------------------------------
# Which platform this is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "sys_platform", "expected"),
    [
        ({"ANDROID_ARGUMENT": "x"}, "linux", "android"),
        ({"ANDROID_PRIVATE": "x"}, "linux", "android"),
        ({"KIVY_BUILD": "ios"}, "darwin", "ios"),
        ({}, "ios", "ios"),
        ({}, "win32", "windows"),
        ({}, "cygwin", "windows"),
        ({}, "darwin", "macos"),
        ({}, "linux", "linux"),
        ({}, "freebsd14", "linux"),
    ],
)
def test_current_platform(env, sys_platform, expected):
    assert current_platform(env=env, sys_platform=sys_platform) == expected


# ---------------------------------------------------------------------------
# Reading the OS install identifier, per platform, with the OS calls mocked
# ---------------------------------------------------------------------------


class FakeWinreg:
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x0100

    def __init__(self, guid="7a1f1e5c-0b3d-4c2a-9e8f-112233445566", fail=False):
        self.guid = guid
        self.fail = fail
        self.opened = []

    def OpenKey(self, root, path, reserved, access):  # noqa: N802 (winreg's own name)
        if self.fail:
            raise OSError("access denied")
        self.opened.append((root, path, reserved, access))
        return SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: False)

    def QueryValueEx(self, key, name):  # noqa: N802
        assert name == "MachineGuid"
        return (self.guid, 1)

    def CloseKey(self, key):  # noqa: N802
        pass


def test_windows_reads_machine_guid_from_the_64_bit_registry_view(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(cp, "_windows_user_sid", lambda: "S-1-5-21-1000")

    ident = read_os_identifier("windows")

    assert ident == OsIdentifier(platform="windows", value=fake.guid, user="S-1-5-21-1000")
    (root, path, _reserved, access) = fake.opened[0]
    assert root == "HKLM"
    assert path == r"SOFTWARE\Microsoft\Cryptography"
    # A 32-bit Python on 64-bit Windows must still read the real value.
    assert access & FakeWinreg.KEY_WOW64_64KEY
    assert access & FakeWinreg.KEY_READ


def test_windows_unreadable_machine_guid_gives_no_value(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg(fail=True))
    monkeypatch.setattr(cp, "_windows_user_sid", lambda: "S-1-5-21-1000")

    assert read_os_identifier("windows").value is None


def test_windows_user_falls_back_to_the_account_name_when_the_sid_cannot_be_read(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg())

    def no_sid():
        raise OSError("no advapi32")

    monkeypatch.setattr(cp, "_windows_user_sid", no_sid)
    monkeypatch.setenv("USERDOMAIN", "OFFICE")
    monkeypatch.setenv("USERNAME", "ann")

    assert read_os_identifier("windows").user == "OFFICE\\ann"


def test_linux_reads_etc_machine_id(tmp_path):
    etc = tmp_path / "machine-id"
    etc.write_text(MACHINE_ID + "\n")

    ident = read_os_identifier("linux", linux_paths=(str(etc), str(tmp_path / "missing")), uid=1000)

    assert ident == OsIdentifier(platform="linux", value=MACHINE_ID, user="1000")


def test_linux_empty_etc_machine_id_falls_back_to_the_dbus_copy(tmp_path):
    etc = tmp_path / "etc-machine-id"
    etc.write_text("")
    dbus = tmp_path / "dbus-machine-id"
    dbus.write_text(OTHER_MACHINE_ID + "\n")

    ident = read_os_identifier("linux", linux_paths=(str(etc), str(dbus)), uid=1000)

    assert ident.value == OTHER_MACHINE_ID


def test_linux_empty_machine_id_as_in_containers_is_not_usable(tmp_path):
    etc = tmp_path / "machine-id"
    etc.write_text("")

    ident = read_os_identifier("linux", linux_paths=(str(etc), str(tmp_path / "missing")), uid=1000)

    assert not usable_identifier(ident.value)


def test_linux_missing_machine_id_gives_no_value(tmp_path):
    ident = read_os_identifier("linux", linux_paths=(str(tmp_path / "a"), str(tmp_path / "b")), uid=0)

    assert ident.value is None
    assert ident.user == "0"


IOREG_OUTPUT = textwrap.dedent(
    """\
    +-o J316sAP  <class IOPlatformExpertDevice, id 0x100000245, registered, matched, active, busy 0 (169 ms)>
        {
          "IOPlatformSerialNumber" = "C02XXXXXXXX"
          "IOPlatformUUID" = "1B2C3D4E-5F60-7182-93A4-B5C6D7E8F901"
          "model" = <"MacBookPro18,1">
        }
    """
)


def test_macos_reads_ioplatformuuid_with_ioreg(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=IOREG_OUTPUT)

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    ident = read_os_identifier("macos", uid=501)

    assert ident == OsIdentifier(platform="macos", value="1B2C3D4E-5F60-7182-93A4-B5C6D7E8F901", user="501")
    assert calls[0][0].endswith("ioreg")
    assert "IOPlatformExpertDevice" in calls[0]
    # The serial number, also in that output, is never what is used.
    assert "C02XXXXXXXX" not in (ident.value or "")


def test_macos_ioreg_failure_gives_no_value(monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError("ioreg")

    monkeypatch.setattr(cp.subprocess, "run", fake_run)

    assert read_os_identifier("macos", uid=501).value is None


def _fake_jnius(android_id):
    class Secure:
        ANDROID_ID = "android_id"

        @staticmethod
        def getString(resolver, key):  # noqa: N802 (Android's own name)
            assert resolver == "resolver"
            assert key == "android_id"
            return android_id

    activity = SimpleNamespace(getContentResolver=lambda: "resolver")
    classes = {
        "android.provider.Settings$Secure": Secure,
        "org.kivy.android.PythonActivity": SimpleNamespace(mActivity=activity),
    }
    return SimpleNamespace(autoclass=lambda name: classes[name])


def test_android_reads_android_id(monkeypatch):
    monkeypatch.setitem(sys.modules, "jnius", _fake_jnius("a1b2c3d4e5f60718"))

    ident = read_os_identifier("android")

    assert ident == OsIdentifier(platform="android", value="a1b2c3d4e5f60718", user="")


def test_android_known_shared_android_id_is_not_usable(monkeypatch):
    # The value a batch of early Android devices all reported.
    monkeypatch.setitem(sys.modules, "jnius", _fake_jnius("9774d56d682e549c"))

    assert not usable_identifier(read_os_identifier("android").value)


def test_android_without_jnius_gives_no_value(monkeypatch):
    monkeypatch.setitem(sys.modules, "jnius", None)

    assert read_os_identifier("android").value is None


def _fake_pyobjus(uuid):
    class NSString:
        def __init__(self, text):
            self.text = text

        def UTF8String(self):  # noqa: N802 (Objective-C's own name)
            return self.text

    vendor_id = None if uuid is None else SimpleNamespace(UUIDString=lambda: NSString(uuid))
    device = SimpleNamespace(identifierForVendor=vendor_id)
    classes = {"UIDevice": SimpleNamespace(currentDevice=lambda: device)}
    return SimpleNamespace(autoclass=lambda name: classes[name])


def test_ios_reads_identifier_for_vendor(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyobjus", _fake_pyobjus("0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0"))

    ident = read_os_identifier("ios")

    assert ident == OsIdentifier(platform="ios", value="0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0", user="")


def test_ios_identifier_for_vendor_not_yet_available_gives_no_value(monkeypatch):
    # Apple: nil after a restart until the device is first unlocked.
    monkeypatch.setitem(sys.modules, "pyobjus", _fake_pyobjus(None))

    assert read_os_identifier("ios").value is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   \n",
        "00000000000000000000000000000000",
        "00000000-0000-0000-0000-000000000000",
        "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
        "uninitialized",
        "03000200-0400-0500-0006-000700080009",
        "9774d56d682e549c",
        "abc",
    ],
)
def test_unusable_identifiers(value):
    assert not usable_identifier(value)


@pytest.mark.parametrize(
    "value",
    [MACHINE_ID, "7a1f1e5c-0b3d-4c2a-9e8f-112233445566", "{7A1F1E5C-0B3D-4C2A-9E8F-112233445566}", "a1b2c3d4e5f60718"],
)
def test_usable_identifiers(value):
    assert usable_identifier(value)


# ---------------------------------------------------------------------------
# Where the small files live: outside the settings file, per OS user
# ---------------------------------------------------------------------------


def test_default_state_dir_per_platform(tmp_path):
    home = tmp_path / "home"
    assert default_state_dir("linux", env={}, home=home) == home / ".local" / "state" / "carvera-controller"
    assert default_state_dir("linux", env={"XDG_STATE_HOME": str(tmp_path / "xdg")}, home=home) == (
        tmp_path / "xdg" / "carvera-controller"
    )
    assert default_state_dir("windows", env={"LOCALAPPDATA": str(tmp_path / "local")}, home=home) == (
        tmp_path / "local" / "CarveraController"
    )
    assert default_state_dir("windows", env={}, home=home) == home / "AppData" / "Local" / "CarveraController"
    assert default_state_dir("macos", env={}, home=home) == (
        home / "Library" / "Application Support" / "CarveraController"
    )


def test_android_state_dir_is_the_no_backup_folder(monkeypatch, tmp_path):
    no_backup = tmp_path / "no_backup"
    activity = SimpleNamespace(getNoBackupFilesDir=lambda: SimpleNamespace(getAbsolutePath=lambda: str(no_backup)))
    classes = {"org.kivy.android.PythonActivity": SimpleNamespace(mActivity=activity)}
    monkeypatch.setitem(sys.modules, "jnius", SimpleNamespace(autoclass=lambda name: classes[name]))

    assert default_state_dir("android", env={}, home=tmp_path) == no_backup / "carvera-controller"


# ---------------------------------------------------------------------------
# Deriving the computer part
# ---------------------------------------------------------------------------


def test_computer_part_is_a_nonzero_64_bit_value(tmp_path):
    part = derive_computer_part("linux", tmp_path, linux_id())

    assert 0 < part.id < 2**64
    assert part.source == "os"
    assert part.copy == 1


def test_computer_part_is_the_same_after_a_restart(tmp_path):
    first = derive_computer_part("linux", tmp_path, linux_id())
    first_id = first.id
    first.release()  # the process exits

    second = derive_computer_part("linux", tmp_path, linux_id())

    assert second.id == first_id
    assert second.copy == 1


def test_computer_part_never_contains_the_raw_identifier(tmp_path):
    part = derive_computer_part("linux", tmp_path, linux_id())
    wire = part.id.to_bytes(8, "big")

    assert wire.hex() not in MACHINE_ID
    assert part.id != int(MACHINE_ID[:16], 16)
    assert MACHINE_ID not in repr(part)
    assert "user" not in repr(part)


def test_reinstall_with_settings_deleted_keeps_the_computer_part(tmp_path):
    """Nothing is read from the settings, so deleting them, or the whole
    state folder, changes nothing while the OS identifier is readable."""
    state = tmp_path / "state"
    first = derive_computer_part("linux", state, linux_id())
    first_id = first.id
    first.release()

    for child in state.iterdir():
        child.unlink()
    state.rmdir()

    assert derive_computer_part("linux", state, linux_id()).id == first_id


def test_two_copies_on_one_computer_get_different_computer_parts(tmp_path):
    first = derive_computer_part("linux", tmp_path, linux_id())
    second = derive_computer_part("linux", tmp_path, linux_id())

    assert first.copy == 1
    assert second.copy == 2
    assert first.id != second.id


def test_the_first_copy_started_again_gets_copy_1_back(tmp_path):
    first = derive_computer_part("linux", tmp_path, linux_id())
    second = derive_computer_part("linux", tmp_path, linux_id())
    first_id, second_id = first.id, second.id

    first.release()
    again = derive_computer_part("linux", tmp_path, linux_id())

    assert again.copy == 1
    assert again.id == first_id
    assert second.id == second_id


def test_a_crashed_copy_never_leaves_its_slot_taken(tmp_path):
    """The operating system drops the lock when the process ends, however
    it ends."""
    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from carveracontroller.machine.computer_part import OsIdentifier, derive_computer_part
        part = derive_computer_part("linux", Path({str(tmp_path)!r}), OsIdentifier("linux", {MACHINE_ID!r}, "1000"))
        print(part.copy, part.id, flush=True)
        time.sleep(60)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        copy, child_id = child.stdout.readline().split()
        assert copy == "1"
        mine = derive_computer_part("linux", tmp_path, linux_id())
        assert mine.copy == 2
        mine.release()
    finally:
        child.kill()
        child.wait()

    after = derive_computer_part("linux", tmp_path, linux_id())
    assert after.copy == 1
    assert after.id == int(child_id)


def test_two_os_users_get_different_computer_parts(tmp_path):
    ann = derive_computer_part("linux", tmp_path / "ann", linux_id(user="1000"))
    bob = derive_computer_part("linux", tmp_path / "bob", linux_id(user="1001"))

    assert ann.id != bob.id


def test_two_os_users_sharing_one_state_folder_still_differ(tmp_path):
    ann = derive_computer_part("linux", tmp_path, linux_id(user="1000"))
    bob = derive_computer_part("linux", tmp_path, linux_id(user="1001"))

    assert ann.copy == bob.copy == 1
    assert ann.id != bob.id


def test_another_computer_gets_a_different_computer_part(tmp_path):
    here = derive_computer_part("linux", tmp_path / "a", linux_id(MACHINE_ID))
    there = derive_computer_part("linux", tmp_path / "b", linux_id(OTHER_MACHINE_ID))

    assert here.id != there.id


def test_the_same_identifier_on_another_platform_differs(tmp_path):
    linux = derive_computer_part("linux", tmp_path / "a", OsIdentifier("linux", MACHINE_ID, "1000"))
    macos = derive_computer_part("macos", tmp_path / "b", OsIdentifier("macos", MACHINE_ID, "1000"))

    assert linux.id != macos.id


def test_identifier_case_and_braces_do_not_matter(tmp_path):
    lower = derive_computer_part("windows", tmp_path / "a", OsIdentifier("windows", "7a1f1e5c-0b3d", "S-1"))
    upper = derive_computer_part("windows", tmp_path / "b", OsIdentifier("windows", "{7A1F1E5C-0B3D}", "S-1"))

    assert lower.id == upper.id


def test_mobile_platforms_take_no_copy_lock(tmp_path):
    first = derive_computer_part("android", tmp_path, OsIdentifier("android", "a1b2c3d4e5f60718", ""))
    second = derive_computer_part("android", tmp_path, OsIdentifier("android", "a1b2c3d4e5f60718", ""))

    assert first.copy == second.copy == 1
    assert first.id == second.id
    assert not list(tmp_path.glob("copy-*"))


def test_a_cloned_disk_image_gives_the_same_computer_part(tmp_path):
    """Clones are identical by construction: same OS identifier, same user,
    first copy on each. The machine refuses the second one while the first is
    connected; making it a separate controller is the way out."""
    original = derive_computer_part("linux", tmp_path / "original", linux_id())
    clone = derive_computer_part("linux", tmp_path / "clone", linux_id())

    assert clone.id == original.id


def test_make_separate_gives_a_clone_its_own_lasting_identity(tmp_path):
    original = derive_computer_part("linux", tmp_path / "original", linux_id())
    clone = derive_computer_part("linux", tmp_path / "clone", linux_id())

    separated = clone.made_separate()

    assert separated.id != original.id
    assert separated.copy == clone.copy
    # Kept outside the settings file, so a restart keeps it.
    clone.release()
    separated.release()
    assert derive_computer_part("linux", tmp_path / "clone", linux_id()).id == separated.id
    # The original is unaffected.
    assert derive_computer_part("linux", tmp_path / "original", linux_id()).id != separated.id


def test_make_separate_again_gives_another_new_identity(tmp_path):
    part = derive_computer_part("linux", tmp_path, linux_id())

    once = part.made_separate()
    twice = once.made_separate()

    assert len({part.id, once.id, twice.id}) == 3


def test_make_separate_keeps_the_copy_slot(tmp_path):
    part = derive_computer_part("linux", tmp_path, linux_id())
    separated = part.made_separate()

    other = derive_computer_part("linux", tmp_path, linux_id())

    assert separated.copy == 1
    assert other.copy == 2


def test_make_separate_on_one_os_user_does_not_affect_another(tmp_path):
    ann = derive_computer_part("linux", tmp_path, linux_id(user="1000"))
    bob_id = derive_computer_part("linux", tmp_path, linux_id(user="1001")).id

    ann.made_separate()
    cp.release_all_for_tests()

    assert derive_computer_part("linux", tmp_path, linux_id(user="1001")).id == bob_id


# ---------------------------------------------------------------------------
# Fallback: no usable identifier (a container's empty /etc/machine-id)
# ---------------------------------------------------------------------------


def test_fallback_is_used_when_the_identifier_is_empty(tmp_path):
    part = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="c0ffee000001")

    assert part.source == "fallback"
    assert 0 < part.id < 2**64
    assert list(tmp_path.glob("fallback-*"))


def test_fallback_survives_a_container_restart(tmp_path):
    """The demo container's state folder is on a persisted volume and a
    restarted container keeps its hostname, so it keeps its identity."""
    first = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="c0ffee000001")
    first_id = first.id
    first.release()

    again = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="c0ffee000001")

    assert again.id == first_id
    assert again.source == "fallback"


def test_fallback_is_not_shared_by_two_containers_on_one_volume(tmp_path):
    a = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="c0ffee000001")
    b = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="c0ffee000002")

    assert a.copy == b.copy == 1
    assert a.id != b.id


def test_containers_on_one_volume_keep_their_own_identity_whatever_order_they_restart(tmp_path):
    a = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="c0ffee000001")
    b = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="c0ffee000002")
    a_id, b_id = a.id, b.id
    a.release()
    b.release()

    b_again = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="c0ffee000002")
    a_again = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="c0ffee000001")

    assert (a_again.id, b_again.id) == (a_id, b_id)


def test_a_copied_fallback_file_does_not_carry_the_identity(tmp_path):
    """The whole state folder copied to another computer with no usable
    identifier either: the other computer's own fingerprint does not match,
    so it makes its own value."""
    here = derive_computer_part("linux", tmp_path / "here", linux_id(value=""), hostname="office-pc")
    here_id = here.id
    there_dir = tmp_path / "there"
    there_dir.mkdir()
    for f in (tmp_path / "here").glob("fallback-*"):
        (there_dir / f.name).write_bytes(f.read_bytes())

    there = derive_computer_part("linux", there_dir, linux_id(value=""), hostname="shop-pc")

    assert there.id != here_id


def test_a_corrupt_fallback_file_is_replaced(tmp_path):
    first = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="h")
    first_id = first.id
    first.release()
    (fallback_file,) = tmp_path.glob("fallback-*")
    fallback_file.write_text("not hex at all")

    again = derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="h")

    assert again.source == "fallback"
    assert again.id != first_id
    again.release()
    assert derive_computer_part("linux", tmp_path, linux_id(value=""), hostname="h").id == again.id


def test_unwritable_state_folder_still_gives_a_unique_identity(tmp_path):
    """Never worse than before on failure: if nothing can be stored, the
    controller still connects, with an identity unique while it runs."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    state = blocker / "state"

    a = derive_computer_part("linux", state, linux_id(value=""), hostname="h")
    b = derive_computer_part("linux", state, linux_id(value=""), hostname="h")

    assert a.source == b.source == "random"
    assert a.id != b.id


def test_copy_lock_failure_still_gives_unique_identities(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")

    a = derive_computer_part("linux", blocker / "state", linux_id())
    b = derive_computer_part("linux", blocker / "state", linux_id())

    assert a.copy is None
    assert a.id != b.id


def test_two_starts_at_once_agree_on_one_fallback_value(tmp_path):
    """Two copies starting together with no fallback file yet: both must use
    the same stored value (and differ only by copy number), or a restart
    would change one of them."""
    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from carveracontroller.machine.computer_part import OsIdentifier, derive_computer_part
        go = float(sys.argv[1])
        while time.time() < go:
            pass
        part = derive_computer_part("linux", Path({str(tmp_path)!r}), OsIdentifier("linux", "", "1000"), hostname="h")
        print(part.copy, flush=True)
        time.sleep(1)
        """
    )
    go = str(time.time() + 1.0)
    children = [subprocess.Popen([sys.executable, "-c", script, go], stdout=subprocess.PIPE, text=True) for _ in "ab"]
    copies = sorted(c.communicate(timeout=30)[0].strip() for c in children)

    assert copies == ["1", "2"]
    assert len(list(tmp_path.glob("fallback-*"))) == 1
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# Override for test rigs
# ---------------------------------------------------------------------------


def test_override_names_the_whole_computer_part(tmp_path):
    a = derive_computer_part("linux", tmp_path / "a", linux_id(), override="rig-seat-7")
    b = derive_computer_part("linux", tmp_path / "b", linux_id(OTHER_MACHINE_ID), override="rig-seat-7")
    c = derive_computer_part("linux", tmp_path / "c", linux_id(), override="rig-seat-8")

    assert a.source == "override"
    assert a.id == b.id
    assert a.id != c.id


def test_this_process_reads_the_override_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(cp.OVERRIDE_ENV, "rig-seat-7")

    part = cp.computer_part_for_this_process(state_dir=tmp_path)

    assert part.source == "override"
    assert part.id == derive_computer_part("linux", tmp_path / "x", linux_id(), override="rig-seat-7").id


def test_this_process_derives_from_the_os(monkeypatch, tmp_path):
    monkeypatch.delenv(cp.OVERRIDE_ENV, raising=False)
    monkeypatch.setattr(cp, "read_os_identifier", lambda platform: OsIdentifier(platform, MACHINE_ID, "1000"))
    monkeypatch.setattr(cp, "current_platform", lambda: "linux")

    part = cp.computer_part_for_this_process(state_dir=tmp_path)

    assert part.source == "os"
    assert part.id == derive_computer_part("linux", tmp_path / "y", linux_id()).id


def test_this_process_never_fails_to_produce_an_identity(monkeypatch, tmp_path):
    monkeypatch.delenv(cp.OVERRIDE_ENV, raising=False)

    def broken(platform):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cp, "read_os_identifier", broken)

    part = cp.computer_part_for_this_process(state_dir=tmp_path)

    assert 0 < part.id < 2**64


# ---------------------------------------------------------------------------
# Launch part
# ---------------------------------------------------------------------------


def test_launch_part_is_start_time_then_random():
    launch = new_launch_part(now=0x12345678)

    assert launch >> 32 == 0x12345678
    assert 0 <= launch < 2**64


def test_two_launches_in_the_same_second_differ():
    assert new_launch_part(now=1000) != new_launch_part(now=1000)


def test_launch_part_uses_the_clock_by_default():
    before = int(time.time())
    launch = new_launch_part()

    assert before <= launch >> 32 <= int(time.time())


def test_lock_files_are_per_identity_and_numbered(tmp_path):
    derive_computer_part("linux", tmp_path, linux_id())
    derive_computer_part("linux", tmp_path, linux_id())

    names = sorted(p.name for p in tmp_path.glob("copy-*"))
    assert len(names) == 2
    assert names[0].endswith("-1.lock")
    assert names[1].endswith("-2.lock")
    assert all(MACHINE_ID not in n for n in names)


def test_state_folder_is_created_private(tmp_path):
    state = tmp_path / "new" / "state"
    derive_computer_part("linux", state, linux_id(value=""), hostname="h")

    if os.name == "posix":
        assert (state.stat().st_mode & 0o077) == 0
