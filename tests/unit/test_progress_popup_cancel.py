"""The progress popup's Cancel button never raises.

makera.kv's Cancel button calls root.cancel(), but ProgressPopup had no
cancel(): progressStart only set one, as an attribute, when given a cancel
callback. A popup opened without one (the config download's) whose button
was then enabled by a progress update crashed the app when pressed
(AttributeError: 'ProgressPopup' object has no attribute 'cancel'), and so
did one left up by a download whose worker had died.

Now ProgressPopup.cancel() runs the callback its opener gave (cancel_func)
or, with none, closes the popup; the callback is forgotten when the popup
closes. The transfers' callback (Makera.cancelProcessingFile) stops a
running transfer, or closes the popup when there is none.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import carveracontroller.main as main_module
from carveracontroller.main import Makera, ProgressPopup


def test_progress_popup_cancel_with_nothing_to_stop_closes_it():
    popup = SimpleNamespace(cancel_func=None, dismiss=MagicMock())

    ProgressPopup.cancel(popup)  # makera.kv's Cancel button: root.cancel()

    popup.dismiss.assert_called_once_with()


def test_progress_popup_cancel_runs_what_its_opener_gave():
    stop = MagicMock()
    popup = SimpleNamespace(cancel_func=stop, dismiss=MagicMock())

    ProgressPopup.cancel(popup)

    stop.assert_called_once_with()
    popup.dismiss.assert_not_called()


def test_progress_popup_forgets_its_cancel_when_it_closes():
    popup = SimpleNamespace(cancel_func=MagicMock())
    ProgressPopup.on_dismiss(popup)
    assert popup.cancel_func is None


def test_progress_start_without_a_cancel_clears_an_earlier_one():
    root = Makera.__new__(Makera)
    root.progress_popup = SimpleNamespace(
        progress_text="", progress_value=0, cancel_func=MagicMock(), btn_cancel=SimpleNamespace(disabled=False)
    )
    root.progress_popup.open = MagicMock()

    Makera.progressStart(root, "Calculating run time estimate...", None)

    assert root.progress_popup.cancel_func is None
    assert root.progress_popup.btn_cancel.disabled is True


@pytest.mark.parametrize(
    ("downloading", "uploading", "has_stream", "stops"),
    [
        (True, False, True, True),
        (False, True, True, True),
        (False, False, True, False),  # the transfer has ended or its worker died
        (True, False, False, False),  # the link was closed under it
    ],
)
def test_cancel_stops_a_running_transfer_or_else_closes_the_popup(downloading, uploading, has_stream, stops):
    root = Makera.__new__(Makera)
    root.downloading = downloading
    root.uploading = uploading
    stream = SimpleNamespace(cancel_process=MagicMock())
    root.controller = SimpleNamespace(stream=stream if has_stream else None)
    root.progressFinish = MagicMock()

    Makera.cancelProcessingFile(root)

    assert stream.cancel_process.called is stops
    assert root.progressFinish.called is not stops


def test_every_method_the_progress_popup_rule_calls_exists():
    kv = (Path(main_module.__file__).parent / "makera.kv").read_text(encoding="utf-8")
    rule = re.search(r"^<ProgressPopup>:\n(.*?)(?=^\S)", kv, re.S | re.M).group(1)
    called = set(re.findall(r"(?<![.\w])root\.(\w+)\(", rule))
    assert called, "the rule no longer calls anything on root"
    for name in called:
        assert callable(getattr(ProgressPopup, name, None)), f"ProgressPopup has no {name}()"
