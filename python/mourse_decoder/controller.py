"""Controller: connects the Rust engine, the decoder and text injection.

A letter flows like this: the Rust worker thread calls ``_on_engine_event``,
the decoder turns the sequence into an Emission, the injector deletes and types,
and the listener gets told for UI feedback. Engine and injector are protocols so
all of this can be tested with fakes, without Rust, a display or real typing.
Scrolling ({Scroll Down} and friends) runs on its own thread outside the macro lock.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import NamedTuple, Protocol

from . import clipboard, desktop, macros
from .decoders import Decoder, Emission, create_decoder
from .dictionary import DictionaryManager
from .function_keys import FunctionKeys, Slot
from .scroller import DOWN, UP, Scroller
from .settings import Settings, effective_typing_delay

log = logging.getLogger(__name__)

# keys in a macro that put exactly one character into the text; any other key ({Ctrl+A}, {Delete}, ...)
# changes the text in ways we cannot follow, so "undo last output" forgets what came before it
_ONE_CHARACTER_KEYS = frozenset({"enter", "return", "eingabe", "tab", "space", "leertaste"})


def typed_length(text: str) -> int:
    """How many Backspaces take ``text`` back: a line break is one Return key however it is written."""
    return len(text.replace("\r\n", "\n").replace("\r", "\n"))


class UndoResult(NamedTuple):
    """Outcome of ``undo_last_output``: ``deleted`` characters, or why nothing was deleted.

    ``reason`` is "" after a deletion, else "nothing" (no output to undo), "busy" (a macro is typing),
    "off" (Morse input is off or the program is stopping) or "modifiers" (Ctrl/Alt/Shift/Win stayed down).
    """

    deleted: int
    reason: str = ""


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
    undo_shortcut: str  # "" = none
    undo_count: int  # how often the undo shortcut was pressed, wraps at 2**32

    def start(self, callback: Callable[[str, str], None], use_hook: bool = True) -> None: ...
    def stop(self) -> None: ...
    def configure(self, dot_threshold_ms: int, letter_gap_ms: int, word_gap_ms: int = 0) -> None: ...
    def reset(self) -> None: ...
    def set_function_keys(self, entries: list[tuple[int, str, bool]]) -> None:
        """Install (slot, trigger, swallow) entries; ValueError if one is invalid or clashes."""
    def clear_function_keys(self) -> None: ...
    def poll_function_key(self) -> int | None:
        """Slot number of the oldest fired function key, None if none is waiting."""
    def function_key_modifiers_released(self) -> bool:
        """True if no Ctrl, Alt, Shift or Win key is held."""
    def poll_abort(self) -> bool:
        """True once after Esc was pressed since the last call; reading clears it."""


class InjectorLike(Protocol):
    def inject_text(self, text: str) -> None: ...
    def type_text(self, text: str) -> None:
        """Always typed key by key, never pasted: for what is meant to go in like keyboard input."""
    def set_typing_delay(self, ms: int) -> None:
        """Optional: the pause after each typed character from now on, in ms."""
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
    def scroll(self, notches: int) -> None:
        """Turn the mouse wheel by ``notches`` clicks; positive is down."""


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
    PASTE_SETTLE_S = 0.5
    # Explorer and mail clients read the file list while they copy, so they need longer
    PASTE_FILES_SETTLE_S = 1.0

    def __init__(self, native_module, clipboard_module=clipboard) -> None:
        self._native = native_module
        self._clipboard = clipboard_module
        self._char_delay_ms: int | None = None  # None = the native default

    def pastes(self, text: str) -> bool:
        # shortcut: Windows only, other systems type everything
        return len(text) >= self.PASTE_FROM and self._clipboard.available()

    def try_paste(self, text: str) -> bool:
        """Paste ``text`` through the clipboard. False if the clipboard was busy and nothing happened."""
        return self._with_saved_clipboard(lambda: self._clipboard.set_text(_crlf(text)), self.PASTE_SETTLE_S)

    def inject_text(self, text: str) -> None:
        if self.pastes(text) and self.try_paste(text):
            return
        self._type_native(text)

    def type_text(self, text: str) -> None:
        self._type_native(text)

    def set_typing_delay(self, ms: int) -> None:
        self._char_delay_ms = ms

    def _type_native(self, text: str) -> None:
        if self._char_delay_ms is None:
            self._native.inject_text(text)
        else:
            self._native.inject_text(text, self._char_delay_ms)

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

    def scroll(self, notches: int) -> None:
        self._native.scroll_wheel(notches)


class MorseController:
    HISTORY_LIMIT = 500
    WINDOW_TIMEOUT_S = 10.0  # for {WaitWindow …}; slow apps like a cold Firefox start need a few seconds
    POLL_S = 0.05  # how often waits look at the window title and the on/off switch
    TYPE_CHUNK = 100  # characters typed between two looks at the on/off switch (about 2 s)
    ERASE_CHUNK = 2000  # Backspaces sent between two looks at the on/off switch and Esc
    MODIFIER_TIMEOUT_S = 3.0  # a function key waits this long for Ctrl/Alt/Shift/Win to be let go

    def __init__(
        self,
        engine: EngineLike,
        injector: InjectorLike,
        dictionaries: DictionaryManager,
        settings: Settings,
        listener: ControllerListener | None = None,
        decoder_factory: Callable[[DictionaryManager, str], Decoder] = create_decoder,
        use_hook: bool = True,
        scroller_factory: Callable[..., Scroller] = Scroller,
    ) -> None:
        self._engine = engine
        self._injector = injector
        self._dictionaries = dictionaries
        self._listener = listener
        self._decoder_factory = decoder_factory
        self._use_hook = use_hook
        self._lock = threading.RLock()
        self._running = False
        self._stopping = False  # set by stop() so a running macro ends before the engine is joined
        self._cycle_seen = 0
        self._undo_seen = 0
        self._settings = settings
        self._function_keys = FunctionKeys()
        # held while a macro types, whether a Morse letter or a function key started it
        self._macro_lock = threading.Lock()
        # Esc is read once from the engine; the counter lets the macro still see an Esc that the
        # scroller thread consumed first
        self._esc_lock = threading.Lock()
        self._esc_generation = 0
        self._macro_esc_mark = 0
        # characters the last output left in the text, for ``undo_last_output``. Written and read only
        # while ``_macro_lock`` is held; ``_run_*`` collect the numbers of the output being typed
        self._last_output = 0
        self._run_typed = 0
        self._run_touched = False
        # lazy wheel: injectors without scroll() work as long as nothing scrolls
        self._scroller = scroller_factory(
            wheel=lambda notches: self._injector.scroll(notches),
            should_stop=self._scroll_stop_reason,
            on_error=lambda message: self._notify("on_error", message),
        )
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
            self._stopping = False
            s = self._settings
            self._engine.configure(s.dot_threshold_ms, s.letter_gap_ms, s.word_gap_ms)
            self._apply_keys(s)
            self._apply_blocking(s)
            self._cycle_seen = int(self._engine.cycle_count)  # the counter lives as long as the process
            self._undo_seen = int(self._engine.undo_count)
            self._engine.start(self._on_engine_event, use_hook=self._use_hook)
            self._running = True
            try:
                self._install_function_keys()
            except ValueError as exc:  # e.g. the Morse key was changed to a trigger a slot uses
                self._notify("on_error", f"Funktionstasten nicht aktiv: {exc}")

    def stop(self) -> None:
        # engine.stop() waits for the worker, which may be blocked on our lock,
        # so flip the flag under the lock but stop outside it
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stopping = True
        self._scroller.stop()
        self._engine.block_input = False  # never leave the key blocked behind
        self._engine.clear_function_keys()
        self._engine.stop()

    def apply_settings(self, new: Settings) -> None:
        errors = new.validate() or self._function_keys.clashes(new)
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
                replaced = self._decoder
                self._decoder = decoder
                self._close_decoder(replaced)
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
        self._engine.undo_shortcut = s.undo_shortcut
        self._engine.block_input = s.block_input
        # the engine only blocks while Morse input is on, so a paused app never eats the key
        self._engine.active = s.enabled

    def poll_active_toggle(self) -> Settings | None:
        """Settings after the shortcut toggled, else None.

        Only ``enabled`` changes, like with the tray menu and the settings switch. ``block_input`` stays the
        saved setting: the engine blocks only while Morse input is on, so a pause never blocks and a resume
        blocks exactly when the setting says so, whichever way it was switched.
        """
        with self._lock:
            s = self._settings
            if not self._running:
                return None
            actual = bool(self._engine.active)
            if actual == s.enabled:
                return None
            return s.replace(enabled=actual)

    def poll_cycle(self) -> int:
        """How often the cycle shortcut was pressed since the last call (0 while stopped)."""
        with self._lock:
            if not self._running:
                return 0
            count = int(self._engine.cycle_count)
            presses, self._cycle_seen = (count - self._cycle_seen) % 2**32, count
            return presses

    def poll_undo(self) -> int:
        """How often the undo shortcut was pressed since the last call (0 while stopped)."""
        with self._lock:
            if not self._running:
                return 0
            count = int(self._engine.undo_count)
            presses, self._undo_seen = (count - self._undo_seen) % 2**32, count
            return presses

    def undo_last_output(self) -> UndoResult:
        """Delete the last output with as many Backspaces as it had characters, once.

        The length is forgotten before anything is sent, so a second call has nothing to delete until
        a new output arrives (and a failing injector cannot lead to deleting twice). Nothing happens
        while a macro types or Morse input is off. Waits up to ``MODIFIER_TIMEOUT_S`` for held
        Ctrl/Alt/Shift/Win keys, because Ctrl+Backspace would delete words.
        """
        with self._lock:
            if not (self._settings.enabled and self._engine.active):
                return UndoResult(0, "off")
        if not self._macro_lock.acquire(blocking=False):
            return UndoResult(0, "busy")
        try:
            if not self._last_output:
                return UndoResult(0, "nothing")
            self._begin_macro()
            try:
                self._wait_for_modifiers_released()
            except macros.MacroStopped:
                return UndoResult(0, "off")
            except macros.MacroError:
                return UndoResult(0, "modifiers")
            count = self._last_output
            self._run_typed, self._run_touched, self._last_output = count, True, 0
            try:
                self._erase()
            except macros.MacroStopped:
                return UndoResult(count - self._run_typed, "off")
            finally:
                self._last_output = self._run_typed  # 0 unless Esc stopped it half way: the rest stays undoable
            return UndoResult(count)
        finally:
            self._macro_lock.release()

    def _erase(self) -> None:
        """Send Backspaces for ``_run_typed`` characters, in pieces of ``ERASE_CHUNK``, counting it down.

        Plain fast Backspaces, no typing delay. Selecting with Shift+Left needs as many key events and
        behaves differently from app to app, so it is not used. Between pieces the on/off switch and Esc
        are looked at; the characters not yet deleted stay in ``_run_typed`` (all forgotten if the injector fails).
        """
        while self._run_typed:
            self._stop_if_switched_off()
            piece = min(self._run_typed, self.ERASE_CHUNK)
            try:
                self._injector.inject_backspaces(piece)
            except Exception:
                self._run_typed = 0  # how much of the piece went out is unknown: never delete again
                raise
            self._run_typed -= piece
            for _ in range(min(piece, len(self.history))):
                self.history.pop()

    def _rollback(self) -> None:
        """``{Rollback}``: delete what this macro typed so far; if it typed or changed nothing yet, the last output."""
        if not self._run_touched:
            self._run_typed, self._last_output = self._last_output, 0
            self._run_touched = True
        self._erase()

    @property
    def function_keys(self) -> FunctionKeys:
        """The slots that were applied last."""
        return self._function_keys

    def check_shortcut(self, combo: str) -> None:
        """ValueError if ``combo`` is no key combination; the UI checks macros with it before saving."""
        self._injector.check_shortcut(combo)

    def apply_function_keys(self, keys: FunctionKeys) -> None:
        """Make ``keys`` the active slots. ValueError (German text) and no change if they are invalid.

        Checks the slots against the current settings (a trigger that the Morse key or a shortcut
        also reacts to is refused) and parses every macro first.
        """
        with self._lock:
            errors = keys.validate(self._settings, self._injector.check_shortcut)
            if errors:
                raise ValueError("; ".join(errors))
            previous = self._function_keys
            self._function_keys = keys
            try:
                self._install_function_keys()
            except ValueError:
                self._function_keys = previous  # the hook refused; it still holds the old table
                raise

    def _install_function_keys(self) -> None:
        """Send the active slots to the hook; only while running, ``start`` calls this again."""
        if not self._running:
            return
        self._engine.set_function_keys([(s.slot, s.trigger, s.swallow) for s in self._function_keys.active_slots()])

    def poll_function_keys(self) -> list[int]:
        """Start the macros of slots whose trigger was pressed since the last call; returns their numbers.

        Does not block: each macro runs on its own thread. A trigger that arrives while another macro
        is typing is dropped. Errors reach the listener's ``on_error``.
        """
        started: list[int] = []
        if not self._running:
            return started
        while (number := self._engine.poll_function_key()) is not None:
            reason = self.run_slot(number)
            if reason is None:
                started.append(number)
            else:
                log.info("Funktionstaste F%d nicht ausgeführt: %s", number, reason)
        return started

    def run_slot(self, number: int, block: bool = False) -> str | None:
        """Run the macro of a slot as if its trigger was pressed; for the test button.

        Returns None when the macro was started (``block=True`` waits until it is done, for tests),
        otherwise the reason why not: another macro runs, Morse input is off, the slot is off or has
        no macro, or its macro is broken. A slot without a trigger can be run this way.
        Waits up to ``MODIFIER_TIMEOUT_S`` for held Ctrl/Alt/Shift/Win keys.
        """
        with self._lock:
            slot = self._function_keys.slot(number)
            if not (slot.enabled and slot.macro.strip()):  # no trigger needed: the test button runs it from the UI
                return f"{slot.name} ist ausgeschaltet oder hat kein Makro."
            if not (self._settings.enabled and self._engine.active):
                return "Die Morse-Eingabe ist ausgeschaltet."
        try:
            macros.parse(slot.macro, self._injector.check_shortcut)  # a broken macro is reported right away
        except macros.MacroError as exc:
            return str(exc)
        if not self._macro_lock.acquire(blocking=False):
            return "Ein anderes Makro läuft noch."
        if block:
            self._run_slot_locked(slot)
        else:
            threading.Thread(target=self._run_slot_locked, args=(slot,), name=f"macro-{slot.name}", daemon=True).start()
        return None

    def _run_slot_locked(self, slot: Slot) -> None:
        """Body of a slot run; the caller holds ``_macro_lock``, released here."""
        try:
            self._begin_macro()
            self._wait_for_modifiers_released()
            self._emit_steps(Emission(slot.macro), slot.typing_delay_ms)
        except macros.MacroStopped as exc:
            log.info("%s: %s", slot.name, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("Fehler in %s: %s", slot.name, exc)
            self._notify("on_error", f"{slot.name}: {exc}")
        finally:
            self._macro_lock.release()

    def _wait_for_modifiers_released(self) -> None:
        """Typed while Ctrl/Alt/Shift/Win are still down, text would turn into shortcuts."""
        deadline = time.monotonic() + self.MODIFIER_TIMEOUT_S
        while not self._engine.function_key_modifiers_released():
            if time.monotonic() > deadline:
                raise macros.MacroError("Ctrl, Alt, Shift oder Win sind noch gedrückt – Makro nicht ausgeführt.")
            time.sleep(self.POLL_S)
            self._stop_if_switched_off()

    def reload_alphabet(self) -> None:
        """Reload the active alphabet after its file changed."""
        with self._lock:
            self._dictionaries.invalidate()
            decoder = self._decoder_factory(self._dictionaries, self._settings.alphabet)  # raises: old decoder stays
            replaced = self._decoder
            self._decoder = decoder
            self._close_decoder(replaced)
            self._engine.reset()

    @staticmethod
    def _close_decoder(old) -> None:
        """Release what a replaced decoder holds (e.g. a database handle); never raises."""
        close = getattr(old, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                log.exception("Decoder konnte nicht geschlossen werden")

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
        # one macro at a time: output of a Morse letter that arrives while a function key types is dropped
        if not self._macro_lock.acquire(blocking=False):
            log.info("Ausgabe verworfen, weil gerade ein Makro tippt")
            self._reset_decoder()  # its state moved on for a letter that never came out
            return
        try:
            self._begin_macro()
            self._emit_steps(emission)
        except BaseException:
            self._reset_decoder()  # stopped or failed half way: the decoder must not build on it
            raise
        finally:
            self._macro_lock.release()

    def _reset_decoder(self) -> None:
        try:
            with self._lock:
                self._decoder.reset()
        except Exception:  # noqa: BLE001 - must not hide the error that got us here
            log.exception("Decoder konnte nicht zurückgesetzt werden")

    def _emit_steps(self, emission: Emission, typing_delay_ms: int | None = None) -> None:
        """Type one output. ``typing_delay_ms`` is the pause after each character of a slot that has its own,
        None = the global setting. A {Delay N} step replaces it for the rest of this output only, because
        every output starts by setting the pause again.

        What the output leaves in the text is counted for ``undo_last_output``: typed characters, and the
        keys Enter, Tab and Space as one each. Any other key, a pasted file or an erase makes the length
        unknown, so it counts as 0 from there on. Scrolling and waiting change nothing and leave the
        length of the previous output alone. Text that Esc cut short is not counted.
        """
        # typing takes a few ms, so do it outside the lock to not block apply_settings
        steps = macros.parse(emission.text, self._injector.check_shortcut)  # first, so a broken macro changes nothing
        self._check_paths(steps)
        self._set_typing_delay(effective_typing_delay(self._settings.typing_delay_ms, typing_delay_ms))
        self._run_typed, self._run_touched = 0, False
        try:
            if emission.erase:
                self._run_touched = True
                self._injector.inject_backspaces(emission.erase)
                for _ in range(min(emission.erase, len(self.history))):
                    self.history.pop()
            for kind, value in steps:
                self._stop_if_switched_off()
                if kind == "text":
                    self._type(value, self._injector.inject_text)
                    self.history.extend(value)
                elif kind == "keys":
                    self._press(value)
                elif kind == "wait":
                    self._sleep(value)
                elif kind == "delay":
                    self._set_typing_delay(value)
                elif kind == "rollback":
                    self._rollback()
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
                elif kind == "scroll":
                    # returns at once: the scroller thread does the rest, outside the macro lock
                    if value == "down":
                        self._scroller.request(DOWN)
                    elif value == "up":
                        self._scroller.request(UP)
                    else:
                        self._scroller.stop()
                else:
                    self._run_touched, self._run_typed = True, 0
                    self._injector.paste_files([macros.resolve_path(p) for p in value])
        finally:
            if self._run_touched:
                self._last_output = self._run_typed
            if len(emission.text) > 1:
                # clicks made while the macro ran are queued in the engine; drop them so
                # they don't come out as stray letters afterwards
                self._engine.reset()

    def _set_typing_delay(self, ms: int) -> None:
        # optional on the injector, like try_paste: fakes and the dry-run printer have no delay
        setter = getattr(self._injector, "set_typing_delay", None)
        if setter is not None:
            setter(ms)

    def _press(self, combo: str) -> None:
        self._run_touched = True
        one_character = combo.strip().lower() in _ONE_CHARACTER_KEYS
        if not one_character:
            self._run_typed = 0
        self._injector.press_shortcut(combo)
        if one_character:
            self._run_typed += 1

    def _type_piece(self, piece: str, inject: Callable[[str], None]) -> None:
        self._run_touched = True  # even a piece that Esc cuts short has changed the text
        inject(piece)
        self._stop_if_switched_off()  # Esc during the piece: the injector stopped early, so it is not counted
        self._run_typed += typed_length(piece)

    def _type(self, text: str, inject: Callable[[str], None], always: bool = False) -> None:
        """Type ``text``; long text in pieces, so switching Morse input off stops it within a moment.

        Text that gets pasted goes in whole, pasting is over in an instant.
        """
        if len(text) <= self.TYPE_CHUNK:
            self._type_piece(text, inject)
            return
        if not always and self._injector.pastes(text):
            try_paste = getattr(self._injector, "try_paste", None)
            self._run_touched = True
            if try_paste is None or try_paste(text):
                if try_paste is None:
                    inject(text)
                self._stop_if_switched_off()
                self._run_typed += typed_length(text)
                return
            # clipboard busy: type it in pieces below, so the off switch and Esc are looked at
        start = 0
        while start < len(text):
            end = min(start + self.TYPE_CHUNK, len(text))
            if text[end - 1 : end] == "\r" and text[end : end + 1] == "\n":
                end += 1  # a split CRLF would be typed as two line breaks
            self._stop_if_switched_off()
            self._type_piece(text[start:end], inject)
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
        if self._stopping:
            raise macros.MacroStopped("Makro gestoppt, weil das Programm beendet wird.")
        # the on/off shortcut flips engine.active right away, also while a macro runs
        if not self._engine.active:
            raise macros.MacroStopped("Makro gestoppt, weil die Morse-Eingabe ausgeschaltet wurde.")
        self._take_esc()  # Esc; the injector already stopped typing on it
        with self._esc_lock:
            pressed = self._esc_generation != self._macro_esc_mark
        if pressed:  # also true if the scroller thread read the flag first
            raise macros.MacroStopped("Makro abgebrochen (Esc).")

    def _read_esc_locked(self) -> bool:
        """Read the engine's Esc flag and count it; the caller holds ``_esc_lock``."""
        pressed = bool(self._engine.poll_abort())
        if pressed:
            self._esc_generation += 1
        return pressed

    def _take_esc(self) -> bool:
        """Read the Esc flag of the engine (the only place that does) and stop scrolling on it."""
        with self._esc_lock:
            pressed = self._read_esc_locked()
        if pressed:
            self._scroller.stop()  # outside the lock
        return pressed

    def _begin_macro(self) -> None:
        # a pending Esc from before still stops scrolling but does not abort this new macro
        with self._esc_lock:
            pressed = self._read_esc_locked()
            self._macro_esc_mark = self._esc_generation
        if pressed:
            self._scroller.stop()

    def _scroll_stop_reason(self) -> str | None:
        """Why scrolling must end now, None if it goes on. Called from the scroller thread."""
        if self._stopping:
            return "Programm wird beendet"
        if not self._engine.active:
            return "Morse-Eingabe ausgeschaltet"
        # While a macro types, Esc is left alone: Rust's injector only peeks the flag to stop typing
        # within one character, and the macro reads it itself (_stop_if_switched_off), which also
        # stops the scroller.
        if not self._macro_lock.locked() and self._take_esc():
            return "Esc"
        return None

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
