"""The countdown shown while the machine holds a job's start, and the texts
for how a held start ended.

Every connected controller shows the countdown, and before it that the
machine is checking the file. Only the controller that started the job
gets Start now. Cancel is shown on every controller the machine accepts an
abort from (see machine/job_start.py's may_cancel_held_start).

When the wait runs out on a machine that then keeps the start held (phase
timed out), the controller that started the job gets Start now, Keep
waiting and Cancel; every other controller sees that the wait ran out and
who is not synced yet, with only the Cancel it already had.
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


def _buttons_line(is_starter: bool, can_cancel: bool) -> str | None:
    if is_starter:
        return tr._("Press Start now to start at once, or Cancel to stop the job from starting.")
    if can_cancel:
        return tr._("Press Cancel to stop the job from starting.")
    return None


def countdown_text(
    path: str, seconds_left: int, waiting_for: Iterable[str], is_starter: bool, can_cancel: bool = False
) -> str:
    """The countdown's text: which file, how long the machine may still
    wait, which controllers it is waiting for, and the buttons this
    controller has."""
    names = ", ".join(waiting_for)
    if seconds_left > 0:
        lines = [tr._("{} will start in {} s.").format(_file_name(path), seconds_left)]
    else:
        lines = [tr._("{} is starting.").format(_file_name(path))]
    if names:
        lines.append(tr._("Waiting for {} to load the file.").format(names))
    buttons = _buttons_line(is_starter, can_cancel)
    if not is_starter:
        lines.append(tr._("The machine will start moving when the countdown ends."))
    if buttons:
        lines.append(buttons)
    return "\n\n".join(lines)


def checking_text(path: str, is_starter: bool, can_cancel: bool = False) -> str:
    """The text while the machine computes the file's MD5 before the
    countdown: which file, that the countdown follows, and the buttons this
    controller has."""
    lines = [
        tr._("Checking {} before the start.").format(_file_name(path)),
        tr._("The machine is reading the file. The countdown begins once it has been checked."),
    ]
    buttons = _buttons_line(is_starter, can_cancel)
    if buttons:
        lines.append(buttons)
    return "\n\n".join(lines)


def timed_out_text(
    path: str,
    not_synced: Iterable[str],
    is_starter: bool,
    can_cancel: bool = False,
    keep_waiting: bool = False,
    starter_name: str | None = None,
) -> str:
    """The text once the wait has run out and the start is held for the
    starting controller's choice: which file, that it has not started,
    which controllers are not synced yet, and either the choice (on the
    starter, with Keep waiting when the machine accepts it) or who makes
    it (on the others, with Cancel if they have it)."""
    names = ", ".join(not_synced)
    lines = [tr._("The wait for the other controllers ran out. {} has not started.").format(_file_name(path))]
    if names:
        lines.append(tr._("Not synced yet: {}.").format(names))
    if is_starter and keep_waiting:
        lines.append(
            tr._(
                "Press Start now to start without them, Keep waiting to wait the full time again, "
                "or Cancel to stop the job."
            )
        )
        lines.append(
            tr._("Until you choose, the job stays held. It starts by itself once every controller has synced.")
        )
        return "\n\n".join(lines)
    if is_starter:
        lines.append(_buttons_line(True, True))
        return "\n\n".join(lines)
    if starter_name:
        lines.append(
            tr._("{} pressed Play and chooses whether to start now, keep waiting or cancel.").format(starter_name)
        )
    else:
        lines.append(tr._("The controller that pressed Play chooses whether to start now, keep waiting or cancel."))
    lines.append(tr._("The job starts by itself once every controller has synced."))
    buttons = _buttons_line(False, can_cancel)
    if buttons:
        lines.append(buttons)
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
    """The countdown. ``update`` refreshes the text and title and shows or
    hides each button; ``on_start_now``, ``on_keep_waiting`` and
    ``on_cancel`` are called from the buttons."""

    def __init__(
        self,
        on_start_now: Callable[[], None],
        on_cancel: Callable[[], None],
        on_keep_waiting: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        self._on_start_now = on_start_now
        self._on_cancel = on_cancel
        self._on_keep_waiting = on_keep_waiting
        content = BoxLayout(orientation="vertical", padding=dp(15), spacing=dp(10))
        self.lb_content = Label(halign="center", valign="middle")
        self.lb_content.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        content.add_widget(self.lb_content)
        self.buttons = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        self.btn_start_now = Button(text=tr._("Start now"))
        self.btn_start_now.bind(on_release=lambda *_: self._on_start_now())
        self.btn_keep_waiting = Button(text=tr._("Keep waiting"))
        self.btn_keep_waiting.bind(on_release=lambda *_: self._keep_waiting())
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

    def update(
        self,
        text: str,
        show_start_now: bool,
        show_cancel: bool,
        show_keep_waiting: bool = False,
        title: str | None = None,
    ) -> None:
        self.lb_content.text = text
        self.title = title if title is not None else tr._("Job about to start")
        self._show_buttons(
            [
                button
                for button, show in (
                    (self.btn_start_now, show_start_now),
                    (self.btn_keep_waiting, show_keep_waiting),
                    (self.btn_cancel, show_cancel),
                )
                if show
            ]
        )
        if not self.showing:
            self.showing = True
            self.open()

    def _show_buttons(self, wanted: list[Button]) -> None:
        """Put exactly `wanted` in the row, left to right in the order
        Start now, Keep waiting, Cancel, so a lone Cancel fills the row; hide
        the row when it is empty."""
        if list(reversed(self.buttons.children)) != wanted:
            self.buttons.clear_widgets()
            for button in wanted:
                self.buttons.add_widget(button)
        self.buttons.disabled = not wanted
        self.buttons.opacity = 1 if wanted else 0

    def _keep_waiting(self) -> None:
        if self._on_keep_waiting is not None:
            self._on_keep_waiting()

    def close(self) -> None:
        if self.showing:
            self.showing = False
            self.dismiss()
