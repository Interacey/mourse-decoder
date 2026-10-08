"""Key button: shows the current Morse key and changes it on click.

Like key binding in games: click the button (say "Space"), then press ANY key or
mouse button (say "Mouse 5"). The input is read by the global Rust hook, not by
Tk, which is why every key and extra mouse buttons like Mouse 4/5 work. Esc cancels.
"""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable
from typing import Protocol

from ..i18n import tr
from ..settings import is_valid_trigger
from ..triggers import (
    CANCEL_TOKEN,
    MODIFIER_ORDER,
    MODIFIER_TOKENS,
    build_shortcut,
    shortcut_label,
    trigger_label,
)
from .theme import Button

POLL_MS = 30
# After capturing, ignore clicks briefly: releasing the mouse button that was just
# assigned (e.g. Mouse 1) must not restart the wait.
REARM_SECONDS = 0.4


class CaptureBackend(Protocol):
    def begin_capture(self) -> None: ...
    def cancel_capture(self) -> None: ...
    def poll_capture(self) -> str | None: ...


def _native_backend() -> CaptureBackend | None:
    from .._native import NATIVE_AVAILABLE, load_native

    return load_native() if NATIVE_AVAILABLE else None  # type: ignore[return-value]


class KeyCaptureButton(Button):
    def __init__(
        self,
        master: tk.Misc,
        trigger: str,
        on_change: Callable[[str], None] | None = None,
        on_hint: Callable[[str], None] | None = None,
        backend: CaptureBackend | None = None,
        min_width: int = 190,
    ) -> None:
        super().__init__(master, trigger_label(trigger), self._toggle, variant="secondary", min_width=min_width)
        self._trigger = trigger
        self._on_change = on_change or (lambda _name: None)
        self._on_hint = on_hint or (lambda _text: None)
        self._backend = backend if backend is not None else _native_backend()
        self._waiting = False
        self._poll_job: str | None = None
        self._stopped_at = 0.0

    @property
    def trigger(self) -> str:
        return self._trigger

    @property
    def waiting(self) -> bool:
        return self._waiting

    def set_trigger(self, trigger: str) -> None:
        """Set from outside (form refresh); ends a running wait."""
        self._trigger = trigger
        self._stop()

    def _toggle(self) -> None:
        if self._waiting:
            self._stop()
            return
        if time.monotonic() - self._stopped_at < REARM_SECONDS:
            return
        if self._backend is None:
            self._on_hint(tr("key.err.native"))
            return
        try:
            self._backend.begin_capture()
        except Exception as exc:  # noqa: BLE001 - hook can't start
            self._on_hint(str(exc))
            return
        self._waiting = True
        self.set_text(tr("key.waiting"))
        self._on_hint(tr("key.hint.waiting"))
        self.grab_set()  # clicks in the app now go to this button instead of other buttons
        self._poll_job = self.after(POLL_MS, self._poll)

    def _poll(self) -> None:
        self._poll_job = None
        if not self._waiting or self._backend is None:
            return
        token = self._backend.poll_capture()
        if token is None:
            self._poll_job = self.after(POLL_MS, self._poll)
        elif token == CANCEL_TOKEN:
            self._stop()
        else:
            self._handle_token(token)

    def _handle_token(self, token: str) -> None:
        if is_valid_trigger(token):
            self._trigger = token
            self._stop()
            self._on_change(token)
        else:  # e.g. the right Windows key: the hook can't use it as a trigger
            self._on_hint(tr("key.unsupported", key=token))
            self._keep_waiting()

    def _keep_waiting(self) -> None:
        """Arm the capture again for the next input."""
        if self._backend is not None:
            self._backend.begin_capture()
        self._poll_job = self.after(POLL_MS, self._poll)

    def _display(self) -> str:
        return trigger_label(self._trigger)

    def _stop(self) -> None:
        was_waiting = self._waiting
        self._waiting = False
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None
        if was_waiting:
            self._stopped_at = time.monotonic()
            if self._backend is not None:
                self._backend.cancel_capture()
            try:
                self.grab_release()
            except tk.TclError:
                pass
        self.set_text(self._display())
        self._on_hint("")

class ShortcutCaptureButton(KeyCaptureButton):
    """Like the key button, but records a combination: hold modifiers, then press the last key.

    The value is a string such as "Ctrl+Shift+Pause" (see ``settings.is_valid_shortcut``).
    """

    def __init__(self, master: tk.Misc, shortcut: str, **kwargs) -> None:
        super().__init__(master, "Pause", **kwargs)
        self._shortcut = shortcut
        self._modifiers: set[str] = set()
        self.set_text(self._display())

    @property
    def shortcut(self) -> str:
        return self._shortcut

    def set_shortcut(self, shortcut: str) -> None:
        self._shortcut = shortcut
        self._modifiers = set()
        self._stop()

    def _display(self) -> str:
        if self._waiting and self._modifiers:
            return " + ".join([m for m in MODIFIER_ORDER if m in self._modifiers] + ["..."])
        return shortcut_label(self._shortcut) or "-"

    def _handle_token(self, token: str) -> None:
        if token in MODIFIER_TOKENS:
            self._modifiers.add(MODIFIER_TOKENS[token])
            self.set_text(self._display())
            self._keep_waiting()
        elif is_valid_trigger(token):
            self._shortcut = build_shortcut(self._modifiers, token)
            self._modifiers = set()
            self._stop()
            self._on_change(self._shortcut)
        else:
            self._on_hint(tr("key.unsupported", key=token))
            self._keep_waiting()

    def _stop(self) -> None:
        if not self._waiting:
            self._modifiers = set()
        super()._stop()
