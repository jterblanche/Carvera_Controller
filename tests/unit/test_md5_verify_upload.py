"""Tests for verifying an uploaded firmware file with the (now-fixed)
"md5sum" command.

Controller.md5Command sends the command; these tests cover the two new
pieces in main.py that use it -- _handle_md5sum_reply, which matches a
console line against an in-flight verification, and _verify_uploaded_md5,
which sends the command and waits (bounded) for a matching reply. Both are
called on the Makera app instance, so tests follow this file's existing
pattern (see test_version_model_query.py) of calling the unbound method
with a SimpleNamespace standing in for self.
"""

import threading
from types import SimpleNamespace

from carveracontroller.Controller import Controller
from carveracontroller.main import Makera


def _host(reply_line=None):
    """A stand-in for the Makera app instance, with a fake md5Command that
    delivers *reply_line* (if given) synchronously, as if the machine had
    already answered by the time the command finishes sending.
    """
    host = SimpleNamespace(
        _md5_verify_event=threading.Event(),
        _md5_verify_expected_path=None,
        _md5_verify_reply=None,
    )
    sent = []

    def fake_md5_command(path):
        sent.append(path)
        if reply_line is not None:
            Makera._handle_md5sum_reply(host, reply_line)

    host.controller = SimpleNamespace(md5Command=fake_md5_command)
    host.sent_commands = sent
    return host


# -----------------------------------------------------------------------
# _handle_md5sum_reply: only acts while a verification is pending, and only
# on a reply for the exact path asked about
# -----------------------------------------------------------------------


def test_reply_ignored_when_no_verification_is_pending():
    host = SimpleNamespace(_md5_verify_expected_path=None, _md5_verify_event=threading.Event(), _md5_verify_reply=None)

    Makera._handle_md5sum_reply(host, "d41d8cd98f00b204e9800998ecf8427e /sd/firmware.bin")

    assert host._md5_verify_reply is None
    assert not host._md5_verify_event.is_set()


def test_reply_for_a_different_path_is_ignored():
    host = SimpleNamespace(
        _md5_verify_expected_path="/sd/firmware.bin",
        _md5_verify_event=threading.Event(),
        _md5_verify_reply=None,
    )

    Makera._handle_md5sum_reply(host, "d41d8cd98f00b204e9800998ecf8427e /sd/other.bin")

    assert host._md5_verify_reply is None
    assert not host._md5_verify_event.is_set()


def test_unrelated_console_line_is_ignored():
    host = SimpleNamespace(
        _md5_verify_expected_path="/sd/firmware.bin",
        _md5_verify_event=threading.Event(),
        _md5_verify_reply=None,
    )

    Makera._handle_md5sum_reply(host, "ok")

    assert host._md5_verify_reply is None
    assert not host._md5_verify_event.is_set()


# -----------------------------------------------------------------------
# _verify_uploaded_md5: the round trip
# -----------------------------------------------------------------------


def test_matching_digest_verifies_true():
    host = _host(reply_line="d41d8cd98f00b204e9800998ecf8427e /sd/firmware.bin")

    result = Makera._verify_uploaded_md5(host, "/sd/firmware.bin", "d41d8cd98f00b204e9800998ecf8427e", timeout=1)

    assert result is True
    assert host.sent_commands == ["/sd/firmware.bin"]


def test_mismatched_digest_verifies_false():
    host = _host(reply_line="00000000000000000000000000000000 /sd/firmware.bin")

    result = Makera._verify_uploaded_md5(host, "/sd/firmware.bin", "d41d8cd98f00b204e9800998ecf8427e", timeout=1)

    assert result is False


def test_file_not_found_verifies_false():
    host = _host(reply_line="File not found: /sd/firmware.bin")

    result = Makera._verify_uploaded_md5(host, "/sd/firmware.bin", "d41d8cd98f00b204e9800998ecf8427e", timeout=1)

    assert result is False


def test_no_reply_is_inconclusive_not_a_failure():
    host = _host(reply_line=None)

    result = Makera._verify_uploaded_md5(host, "/sd/firmware.bin", "d41d8cd98f00b204e9800998ecf8427e", timeout=0.05)

    assert result is None


def test_disconnect_while_waiting_is_also_inconclusive():
    # The disconnect handler wakes a waiting call early by setting the
    # event without supplying a reply, same as a genuine timeout.
    host = _host(reply_line=None)
    host.controller.md5Command = lambda path: host._md5_verify_event.set()

    result = Makera._verify_uploaded_md5(host, "/sd/firmware.bin", "d41d8cd98f00b204e9800998ecf8427e", timeout=1)

    assert result is None


# -----------------------------------------------------------------------
# The two sides agree on one path form
#
# The machine echoes back the path it resolved, so the string waited for
# has to be the string sent. On Windows the caller's path arrives spelt
# with backslashes while the command converts them to forward slashes, so
# a second, separate conversion (or none) on the waiting side would never
# match and every verification would time out. These tests use a path
# spelt the Windows way rather than trying to run as Windows, so they
# catch that on any platform.
# -----------------------------------------------------------------------


def test_a_windows_spelt_path_matches_the_reply_the_machine_sends():
    host = _host(reply_line="d41d8cd98f00b204e9800998ecf8427e /sd/firmware.bin")

    result = Makera._verify_uploaded_md5(host, "\\sd\\firmware.bin", "d41d8cd98f00b204e9800998ecf8427e", timeout=1)

    assert result is True
    assert host.sent_commands == ["/sd/firmware.bin"]


def test_a_windows_spelt_path_still_recognises_file_not_found():
    host = _host(reply_line="File not found: /sd/firmware.bin")

    result = Makera._verify_uploaded_md5(host, "\\sd\\firmware.bin", "d41d8cd98f00b204e9800998ecf8427e", timeout=1)

    assert result is False


def test_the_path_waited_for_is_the_text_the_real_command_sends():
    # Not a stub controller: the real md5Command builds the line, and the
    # expected path is read at the moment it is sent. Asserting the two
    # against each other is what a separate "the command converts" test
    # and a separate "the matcher accepts slashes" test cannot do -- both
    # of those passed while the two sides disagreed. Reading the expected
    # path here also shows it is set before the command goes out, which it
    # must be: the reply is matched on the monitor thread.
    host = _host()
    seen = {}

    def record(text):
        seen["text"] = text
        seen["expected"] = host._md5_verify_expected_path

    controller = Controller.__new__(Controller)  # skip __init__: no stream needed
    controller.executeCommand = record
    host.controller = controller

    Makera._verify_uploaded_md5(host, "\\sd\\firmware.bin", "d41d8cd98f00b204e9800998ecf8427e", timeout=0.05)

    assert seen["expected"] == "/sd/firmware.bin"
    assert seen["text"] == "md5sum %s\n" % seen["expected"]


def test_expected_path_is_cleared_after_the_call_either_way():
    host = _host(reply_line="d41d8cd98f00b204e9800998ecf8427e /sd/firmware.bin")

    Makera._verify_uploaded_md5(host, "/sd/firmware.bin", "d41d8cd98f00b204e9800998ecf8427e", timeout=1)

    assert host._md5_verify_expected_path is None
