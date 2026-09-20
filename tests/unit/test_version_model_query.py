"""Tests for the version/model query loop fix.

Covers two related bugs, both in check_model_metadata()'s handling of the
machine's "version"/"model" replies (main.py):

1. The old version-reply regex only matched a release-style
   "major.minor.patch" number, so a firmware built from a branch (which
   reports "<branch>-<commit>") was never recognised. fw_version stayed
   empty forever and check_model_metadata queried "version" again on every
   10-second tick, indefinitely.
2. Nothing capped how many times check_model_metadata would query "version"
   or "model" while waiting for a first reply, so a machine that never
   replies at all (to either command) was also queried forever.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from carveracontroller.main import MACHINE_METADATA_QUERY_MAX_ATTEMPTS, Makera
from carveracontroller.Utils import digitize_v


def _host(fw_version=""):
    return SimpleNamespace(
        fw_version=fw_version,
        fw_version_query_attempts=0,
        fw_version_unknown=False,
        model_query_attempts=0,
        model_unknown=False,
        controller=SimpleNamespace(
            stream=object(),
            viewWCS=Mock(),
            is_community_firmware=False,
            queryVersion=Mock(),
            queryModel=Mock(),
        ),
        onFirmwareDetected=Mock(),
    )


def _fake_app(model=""):
    return SimpleNamespace(model=model, is_community_firmware=False, fw_version_digitized=0)


# -----------------------------------------------------------------------
# _handle_version_reply: what gets recorded for each reply shape
# -----------------------------------------------------------------------


def test_release_style_reply_is_recorded_and_digitized():
    host = _host()
    app = _fake_app()
    with patch("carveracontroller.main.App.get_running_app", return_value=app):
        Makera._handle_version_reply(host, "version = 2.2.0c\n")

    assert host.fw_version == "2.2.0c"
    assert app.is_community_firmware is True
    assert app.fw_version_digitized == digitize_v("2.2.0c")


def test_development_reply_is_recorded_as_the_raw_string():
    # Firmware built from a branch reports "<branch>-<commit>"
    # (src/generate-version.sh), not a release number. This is the reply
    # that used to be silently dropped, leaving fw_version empty forever.
    host = _host()
    app = _fake_app()
    dev_version = "feat/wifi-max-clients-3e593c7"
    with patch("carveracontroller.main.App.get_running_app", return_value=app):
        Makera._handle_version_reply(host, f"version = {dev_version}\n")

    assert host.fw_version == dev_version
    # digitize_v() has no digits to find and safely falls back to 0: older
    # than every release, so every ">= threshold" feature gate reads this
    # build as it would a genuinely old machine, and stays off.
    assert app.fw_version_digitized == 0


def test_development_reply_without_the_letter_c_is_not_flagged_community():
    # is_community_firmware is a plain substring test for "c" in the version
    # string; that is pre-existing, unrelated to this fix, and not changed
    # here. It simply now runs for development replies too, since they are
    # no longer dropped before reaching it. A branch/commit string with no
    # "c" in it demonstrates the safe direction that gives: not flagged.
    host = _host()
    app = _fake_app()
    with patch("carveracontroller.main.App.get_running_app", return_value=app):
        Makera._handle_version_reply(host, "version = fix/wifi-limit-abf1234\n")

    assert app.is_community_firmware is False


def test_malformed_or_empty_reply_is_ignored():
    host = _host(fw_version="previous")
    app = _fake_app()
    with patch("carveracontroller.main.App.get_running_app", return_value=app):
        # No value after "version = " at all.
        Makera._handle_version_reply(host, "version = \n")
        # Not a version reply at all.
        Makera._handle_version_reply(host, "ok\n")

    # Untouched: nothing to record, so the previous value (or lack of one)
    # is left alone rather than being overwritten with something blank.
    assert host.fw_version == "previous"


# -----------------------------------------------------------------------
# check_model_metadata: the retry cap that stops the infinite query
# -----------------------------------------------------------------------


def test_version_query_stops_after_max_attempts_when_there_is_no_reply():
    host = _host()
    app = _fake_app(model="CA1")  # model already known; isolate the version path

    with patch("carveracontroller.main.App.get_running_app", return_value=app):
        # Simulate the 10-second timer firing many times with the machine
        # never answering "version" at all.
        for _ in range(MACHINE_METADATA_QUERY_MAX_ATTEMPTS + 5):
            Makera.check_model_metadata(host)

    assert host.controller.queryVersion.call_count == MACHINE_METADATA_QUERY_MAX_ATTEMPTS
    assert host.fw_version_unknown is True
    assert host.fw_version == ""


def test_model_query_stops_after_max_attempts_when_there_is_no_reply():
    host = _host(fw_version="2.2.0c")  # version already known; isolate the model path
    app = _fake_app(model="")

    with patch("carveracontroller.main.App.get_running_app", return_value=app):
        for _ in range(MACHINE_METADATA_QUERY_MAX_ATTEMPTS + 5):
            Makera.check_model_metadata(host)

    assert host.controller.queryModel.call_count == MACHINE_METADATA_QUERY_MAX_ATTEMPTS
    assert host.model_unknown is True
    assert app.model == ""


def test_version_query_stops_as_soon_as_a_reply_is_recorded():
    host = _host()
    app = _fake_app(model="CA1")

    with patch("carveracontroller.main.App.get_running_app", return_value=app):
        Makera.check_model_metadata(host)
        assert host.controller.queryVersion.call_count == 1

        # The reply arrives (any non-empty value, release or development).
        host.fw_version = "2.2.0c"

        for _ in range(5):
            Makera.check_model_metadata(host)

    # No further queries once a value is known, and no false "unknown".
    assert host.controller.queryVersion.call_count == 1
    assert host.fw_version_unknown is False


def test_check_model_metadata_does_nothing_while_disconnected():
    host = _host()
    host.controller.stream = None
    app = _fake_app()

    with patch("carveracontroller.main.App.get_running_app", return_value=app):
        Makera.check_model_metadata(host)

    host.controller.queryVersion.assert_not_called()
    host.controller.queryModel.assert_not_called()
