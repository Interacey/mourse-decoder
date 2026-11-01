"""Controller: connects the Rust engine, the decoder and text injection.

A letter flows like this: the Rust worker thread calls ``_on_engine_event``,
the decoder turns the sequence into an Emission, the injector deletes and types,
and the listener gets told for UI feedback. Engine and injector are protocols so
all of this can be tested with fakes, without Rust, a display or real typing.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

from . import clipboard, desktop, macros
from .decoders import Decoder, Emission, create_decoder
from .dictionary import DictionaryManager
from .settings import Settings

log = logging.getLogger(__name__)


def _crlf(text: str) -> str:
    """Every line break (LF, CR or CRLF) as CRLF: Windows apps show a bare LF or CR as nothing."""
    return re.sub(r"\r\n|\r|\n", "\r\n", text)


class EngineLike(Protocol):
    """The part of ``_morse_core.MorseEngine`` the controller uses."""

    trigger: str
    dash_trigger: str  # "" = one key, the press duration decides
    block_input: bool
    active: bool
    block_shortcut: str
    cycle_shortcut: str  # "" = none
    cycle_count: int  # how often the cycle shortcut was pressed, wraps at 2**32

    def start(self, callback: Callable[[str, str], None], use_hook: bool = True) -> None: ...
    def stop(self) -> None: ...
    def configure(self, dot_threshold_ms: int, letter_gap_ms: int, word_gap_ms: int = 0) -> None: ...
    def reset(self) -> None: ...


class InjectorLike(Protocol):
    def inject_text(self, text: str) -> None: ...
    def type_text(self, text: str) -> None:
        """Always typed key by key, never pasted: for what is meant to go in like keyboard input."""
    def pastes(self, text: str) -> bool:
        """True if ``inject_text`` would paste ``text`` instead of typing it."""
    def inject_backspaces(self, count: int) -> None: ...
    def press_shortcut(self, combo: str) -> None: ...
    def check_shortcut(self, combo: str) -> None:
        """ValueError if ``combo`` is no key combination such as "Ctrl+C"; presses nothing."""
    def foreground_title(self) -> str: ...
    def clipboard_text(self) -> str | None:
        """The text on the clipboard right now, None if there is none."""
    def paste_files(self, paths: list[str]) -> None:
        """Paste files or folders into the window in front, as if copied in Explorer."""


class ControllerListener(Protocol):
    """Optional UI feedback. These are called on the worker thread."""

    def on_symbol(self, symbol: str) -> None: ...
    def on_letter(self, sequence: str, text: str | None) -> None: ...
    def on_error(self, message: str) -> None: ...


class NativeInjector:
    """Adapter for the Rust ``inject_text`` / ``inject_backspaces`` / ``press_shortcut`` functions."""

    # Everything is typed like keyboard input; only really long texts are pasted, because
    # typing takes about 20 ms per character (2000 characters are 40 seconds)
    PASTE_FROM = 2000
    # the app reads the clipboard only after Ctrl+V arrives, restoring earlier pastes the old content
    PASTE_SETTLE_S = 0.3
    # Explorer and mail clients read the file list while they copy, so they need longer
    PASTE_FILES_SETTLE_S = 1.0

    def __init__(self, native_module, clipboard_module=clipboard) -> None:
        self._native = native_module
        self._clipboard = clipboard_module

    def pastes(self, text: str) -> bool:
        # shortcut: Windows only, other systems type everything
        return len(text) >= self.PASTE_FROM and self._clipboard.available()

    def inject_text(self, text: str) -> None:
        if self.pastes(text):
            if self._with_saved_clipboard(lambda: self._clipboard.set_text(_crlf(text)), self.PASTE_SETTLE_S):
                return
        self._native.inject_text(text)

    def type_text(self, text: str) -> None:
        self._native.inject_text(text)
    def _with_saved_clipboard(self, fill, settle_s: float) -> bool:
        """Fill the clipboard, press Ctrl+V and put the old content back. False if it couldn't be saved."""
        try:
            saved = self._clipboard.save()
        except OSError:
            return False  # clipboard busy: never paste over content we couldn't save
        try:
            fill()
            self._native.press_shortcut("Ctrl+V")
            time.sleep(settle_s)
        finally:
            self._clipboard.restore(saved)
        return True

    def clipboard_text(self) -> str | None:
        if not self._clipboard.available():
            raise macros.MacroError("Die Zwischenablage lässt sich nur unter Windows lesen.")
        try:
            return self._clipboard.get_text()
        except OSError as exc:
            raise macros.MacroError(f"Zwischenablage nicht lesbar: {exc}") from None

    def paste_files(self, paths: list[str]) -> None:
        if not self._clipboard.available():
            raise macros.MacroError("Dateien einfügen geht nur unter Windows.")
        for path in paths:  # whoever calls this, a network path is never put on the clipboard
            macros.resolve_path(path)
        if not self._with_saved_clipboard(lambda: self._clipboard.set_files(paths), self.PASTE_FILES_SETTLE_S):
            raise macros.MacroError("Zwischenablage ist belegt – Dateien nicht eingefügt.")

    def inject_backspaces(self, count: int) -> None:
        self._native.inject_backspaces(count)

    def press_shortcut(self, combo: str) -> None:
        self._native.press_shortcut(combo)

    def check_shortcut(self, combo: str) -> None:
        self._native.check_shortcut(combo)

    def foreground_title(self) -> str:
        return desktop.foreground_title()


class MorseController:
    HISTORY_LIMIT = 500
    WINDOW_TIMEOUT_S = 10.0  # for {WaitWindow …}; slow apps like a cold Firefox start need a few seconds
    POLL_S = 0.05  # how often waits look at the window title and the on/off switch
    TYPE_CHUNK = 100  # characters typed between two looks at the on/off switch (about 2 s)

    def __init__(
        self,
        engine: EngineLike,
        injector: InjectorLike,
        dictionaries: DictionaryManager,
        settings: Settings,
        listener: ControllerListener | None = None,
        decoder_factory: Callable[[DictionaryManager, str], Decoder] = create_decoder,
        use_hook: bool = True,
    ) -> None:
        self._engine = engine
        self._injector = injector
        self._dictionaries = dictionaries
        self._listener = listener
        self._decoder_factory = decoder_factory
        self._use_hook = use_hook
        self._lock = threading.RLock()
        self._running = False
        self._cycle_seen = 0
        self._settings = settings
        try:
            dictionaries.set_smileys(settings.smileys)
        except ValueError as exc:  # set deleted or broken: start without smileys instead of crashing
            log.warning("Smiley-Set '%s' nicht ladbar (%s) – Smileys aus", settings.smileys, exc)
        # build the decoder now so a bad alphabet fails at startup, not on the first letter
        self._decoder: Decoder = decoder_factory(dictionaries, settings.alphabet)
        # one entry per character so ``erase`` can pop entries 1:1; maxlen caps memory
        self.history: deque[str] = deque(maxlen=self.HISTORY_LIMIT)

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def running(self) -> bool:
        return self._running

    def set_listener(self, listener: ControllerListener | None) -> None:
        self._listener = listener

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            s = self._settings
            self._engine.configure(s.dot_threshold_ms, s.letter_gap_ms, s.word_gap_ms)
            self._apply_keys(s)
            self._apply_blocking(s)
            self._cycle_seen = int(self._engine.cycle_count)  # the counter lives as long as the process
            self._engine.start(self._on_engine_event, use_hook=self._use_hook)
            self._running = True

    def stop(self) -> None:
        # engine.stop() waits for the worker, which may be blocked on our lock,
        # so flip the flag under the lock but stop outside it
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._engine.block_input = False  # never leave the key blocked behind
        self._engine.stop()

    def apply_settings(self, new: Settings) -> None:
        errors = new.validate()
        if errors:
            raise ValueError("; ".join(errors))
        with self._lock:
            old = self._settings
            if new.alphabet != old.alphabet or new.smileys != old.smileys:
                # build the new decoder first, if that fails nothing changes
                try:
                    self._dictionaries.set_smileys(new.smileys)
                    decoder = self._decoder_factory(self._dictionaries, new.alphabet)
                except Exception:
                    self._dictionaries.set_smileys(old.smileys)
                    raise
                self._decoder = decoder
                self._engine.reset()  # a half-keyed sequence belongs to the old alphabet
            timing_changed = (old.dot_threshold_ms, old.letter_gap_ms, old.word_gap_ms) != (
                new.dot_threshold_ms,
                new.letter_gap_ms,
                new.word_gap_ms,
            )
            if timing_changed:
                self._engine.configure(new.dot_threshold_ms, new.letter_gap_ms, new.word_gap_ms)
            # only on a real change: rebuilding the keys would forget a press in progress
            if (new.trigger, new.engine_dash_trigger) != (old.trigger, old.engine_dash_trigger):
                self._apply_keys(new)
            self._apply_blocking(new)
            self._settings = new

    def _apply_keys(self, s: Settings) -> None:
        self._engine.trigger = s.trigger
        self._engine.dash_trigger = s.engine_dash_trigger

    def _apply_blocking(self, s: Settings) -> None:
        self._engine.block_shortcut = s.block_shortcut
        self._engine.cycle_shortcut = s.cycle_shortcut
        self._engine.block_input = s.block_input
        # the engine only blocks while Morse input is on, so a paused app never eats the key
        self._engine.active = s.enabled

    def poll_active_toggle(self) -> Settings | None:
        """Settings after the shortcut toggled, else None.

        The shortcut flips both switches together: Morse input and blocking of the normal input.
        """
        with self._lock:
            s = self._settings
            if not self._running:
                return None
            actual = bool(self._engine.active)
            if actual == s.enabled:
                return None
            return s.replace(enabled=actual, block_input=actual)

    def poll_cycle(self) -> int:
        """How often the cycle shortcut was pressed since the last call (0 while stopped)."""
        with self._lock:
            if not self._running:
                return 0
            count = int(self._engine.cycle_count)
            presses, self._cycle_seen = (count - self._cycle_seen) % 2**32, count
            return presses

    def reload_alphabet(self) -> None:
        """Reload the active alphabet after its file changed."""
        with self._lock:
            self._dictionaries.invalidate()
            self._decoder = self._decoder_factory(self._dictionaries, self._settings.alphabet)
            self._engine.reset()

    # engine events arrive on the Rust worker thread
    def _on_engine_event(self, kind: str, payload: str) -> None:
        # an exception here wouldn't kill the worker, but the letter would vanish silently
        try:
            if kind == "symbol":
                self._notify("on_symbol", payload)
            elif kind == "letter":
                self._handle_letter(payload)
            elif kind == "word_gap":
                self._handle_word_gap()
            else:
                log.debug("Unbekanntes Engine-Ereignis %r", kind)
        except macros.MacroStopped:
            log.info("Makro gestoppt, weil die Morse-Eingabe ausgeschaltet wurde")
        except Exception as exc:  # noqa: BLE001
            log.exception("Fehler bei Engine-Ereignis %s", kind)
            self._notify("on_error", str(exc))

    def _handle_letter(self, sequence: str) -> None:
        with self._lock:
            if not self._settings.enabled:
                return  # engine keeps running, output is paused
            emission = self._decoder.decode(sequence)
        if emission is None:
            self._notify("on_letter", sequence, None)
            return
        self._emit(emission)
        self._notify("on_letter", sequence, emission.text)

    def _handle_word_gap(self) -> None:
        with self._lock:
            if not self._settings.enabled:
                return
            emission = self._decoder.word_gap()
        if emission is not None:
            self._emit(emission)

    def _emit(self, emission: Emission) -> None:
        # typing takes a few ms, so do it outside the lock to not block apply_settings
        steps = macros.parse(emission.text, self._injector.check_shortcut)  # first, so a broken macro changes nothing
        self._check_paths(steps)
        if emission.erase:
            self._injector.inject_backspaces(emission.erase)
            for _ in range(min(emission.erase, len(self.history))):
                self.history.pop()
        try:
            for kind, value in steps:
                self._stop_if_switched_off()
                if kind == "text":
                    self._type(value, self._injector.inject_text)
                    self.history.extend(value)
                elif kind == "keys":
                    self._injector.press_shortcut(value)
                elif kind == "wait":
                    self._sleep(value)
                elif kind == "window":
                    self._wait_for_window(value)
                elif kind == "clipboard":
                    # always typed, never pasted: it goes in like keyboard input, and password
                    # fields often refuse a paste. Not added to the history, which the live
                    # page shows, because it is often a password.
                    secret = self._injector.clipboard_text()
                    if not secret:
                        raise macros.MacroError("Die Zwischenablage enthält keinen Text – nichts getippt.")
                    self._type(secret, self._injector.type_text, always=True)
                else:
                    self._injector.paste_files([macros.resolve_path(p) for p in value])
        finally:
            if len(emission.text) > 1:
                # clicks made while the macro ran are queued in the engine; drop them so
                # they don't come out as stray letters afterwards
                self._engine.reset()

    def _type(self, text: str, inject: Callable[[str], None], always: bool = False) -> None:
        """Type ``text``; long text in pieces, so switching Morse input off stops it within a moment.

        Text that gets pasted goes in whole, pasting is over in an instant.
        """
        if len(text) <= self.TYPE_CHUNK or (not always and self._injector.pastes(text)):
            inject(text)
            return
        start = 0
        while start < len(text):
            end = min(start + self.TYPE_CHUNK, len(text))
            if text[end - 1 : end] == "\r" and text[end : end + 1] == "\n":
                end += 1  # a split CRLF would be typed as two line breaks
            self._stop_if_switched_off()
            inject(text[start:end])
            start = end

    @staticmethod
    def _check_paths(steps: list[macros.Step]) -> None:
        """A missing file stops the macro before anything is typed, like a typo in a command."""
        for kind, value in steps:
            if kind == "files":
                for path in value:
                    if not os.path.exists(macros.resolve_path(path)):  # raises first for a network path
                        raise macros.MacroError(f"Datei oder Ordner nicht gefunden: {path}. Nichts getippt.")

    def _stop_if_switched_off(self) -> None:
        # the on/off shortcut flips engine.active right away, also while a macro runs
        if not self._engine.active:
            raise macros.MacroStopped("Makro gestoppt, weil die Morse-Eingabe ausgeschaltet wurde.")

    def _sleep(self, seconds: float) -> None:
        """time.sleep in small steps, so switching Morse input off stops a long wait."""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            time.sleep(min(left, self.POLL_S))
            self._stop_if_switched_off()

    def _wait_for_window(self, title_part: str) -> None:
        deadline = time.monotonic() + self.WINDOW_TIMEOUT_S
        while title_part.casefold() not in self._injector.foreground_title().casefold():
            if time.monotonic() > deadline:
                raise macros.MacroError(
                    f"Kein Fenster mit '{title_part}' im Titel erschienen – Rest des Makros nicht ausgeführt."
                )
            time.sleep(self.POLL_S)
            self._stop_if_switched_off()

    def _notify(self, method: str, *args: object) -> None:
        if self._listener is None:
            return
        try:
            getattr(self._listener, method)(*args)
        except Exception:  # noqa: BLE001 - a UI error must not stop the morsing
            log.exception("Listener-Fehler in %s", method)

    @property
    def typed_text(self) -> str:
        """Everything typed since start, after corrections."""
        return "".join(self.history)
