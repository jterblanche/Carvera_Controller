"""The countdown shown while the machine holds a job's start, and the texts
for how a held start ended.

Every connected controller shows the countdown. Only the controller that
started the job gets the Start now and Cancel buttons.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterable

from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup

from .. import Utils
from ..protocols.handshake import (
    JOB_START_REASON_ABORT,
    JOB_START_REASON_HALT,
    JOB_START_REASON_STARTER_LEFT,
    JOB_START_REASON_TIME_LIMIT,
)
from ..translation import tr


def _file_name(path: str) -> str:
    return posixpath.basename(path) or path


def countdown_text(path: str, seconds_left: int, waiting_for: Iterable[str], is_starter: bool) -> str:
    """The countdown's text: which file, how long the machine may still
    wait, and which controllers it is waiting for."""
    names = ", ".join(waiting_for)
    if seconds_left > 0:
        lines = [tr._("{} will start in {} s.").format(_file_name(path), seconds_left)]
    else:
        lines = [tr._("{} is starting.").format(_file_name(path))]
    if names:
        lines.append(tr._("Waiting for {} to load the file.").format(names))
    if is_starter:
        lines.append(tr._("Press Start now to start at once, or Cancel to stop the job from starting."))
    else:
        lines.append(tr._("The machine will start moving when the countdown ends."))
    return "\n\n".join(lines)


def cancelled_text(path: str, reason: int) -> str:
    """The message shown on every controller when a held start is
    cancelled, with the reason the machine gave."""
    if reason == JOB_START_REASON_ABORT:
        why = tr._("It was cancelled from a controller.")
    elif reason == JOB_START_REASON_STARTER_LEFT:
        why = tr._("The controller that started it disconnected.")
    elif reason == JOB_START_REASON_HALT:
        why = tr._("The machine halted.")
    else:
        why = tr._("The machine cancelled it.")
    return tr._("The job {} did not start.").format(_file_name(path)) + "\n\n" + why


def started_without_text(path: str, reason: int, names: Iterable[str]) -> str:
    """The console note when a job starts while some controllers are still
    not ready."""
    names_text = ", ".join(names)
    if reason == JOB_START_REASON_TIME_LIMIT:
        return tr._("{} started at the time limit without waiting for: {}").format(_file_name(path), names_text)
    return tr._("{} started without waiting for: {}").format(_file_name(path), names_text)


def not_loaded_text(path: str) -> str:
    """The console note when this controller could not load the job's file
    before the start."""
    return tr._(
        "Could not load {} before the job started, so its toolpath is not shown. It will be fetched when the job ends."
    ).format(_file_name(path))


def no_toolpath_progress_text(path: str, played_lines: int, played_seconds: float) -> str:
    """The progress text while a job plays whose file this controller could
    not draw: the name, a plain note that the toolpath is not loaded, the
    lines played and the time elapsed."""
    return " " + tr._("{} (toolpath not loaded): line {}, {} elapsed").format(
        _file_name(path), played_lines, Utils.second2hour(int(played_seconds))
    )


class JobStartPopup(Popup):
    """The countdown. ``update`` refreshes the text and shows or hides the
    buttons; ``on_start_now`` and ``on_cancel`` are called from the
    buttons."""

    def __init__(self, on_start_now: Callable[[], None], on_cancel: Callable[[], None], **kwargs) -> None:
        self._on_start_now = on_start_now
        self._on_cancel = on_cancel
        content = BoxLayout(orientation="vertical", padding=dp(15), spacing=dp(10))
        self.lb_content = Label(halign="center", valign="middle")
        self.lb_content.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        content.add_widget(self.lb_content)
        self.buttons = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        self.btn_start_now = Button(text=tr._("Start now"))
        self.btn_start_now.bind(on_release=lambda *_: self._on_start_now())
        self.btn_cancel = Button(text=tr._("Cancel"))
        self.btn_cancel.bind(on_release=lambda *_: self._on_cancel())
        self.buttons.add_widget(self.btn_start_now)
        self.buttons.add_widget(self.btn_cancel)
        content.add_widget(self.buttons)
        super().__init__(
            title=tr._("Job about to start"),
            content=content,
            size_hint=(0.55, 0.45),
            auto_dismiss=False,
            **kwargs,
        )
        self.showing = False

    def update(self, text: str, show_buttons: bool) -> None:
        self.lb_content.text = text
        self.buttons.disabled = not show_buttons
        self.buttons.opacity = 1 if show_buttons else 0
        if not self.showing:
            self.showing = True
            self.open()

    def close(self) -> None:
        if self.showing:
            self.showing = False
            self.dismiss()
