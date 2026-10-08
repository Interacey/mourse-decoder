"""Controller: connects the Rust engine, the decoder and text injection.

A letter flows like this: the Rust worker thread calls ``_on_engine_event``,
the decoder turns the sequence into an Emission, the injector deletes and types,
and the listener gets told for UI feedback. Engine and injector are protocols so
all of this can be tested with fakes, without Rust, a display or real typing.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Protocol

from .decoders import Decoder, Emission, create_decoder
from .dictionary import DictionaryManager
from .settings import Settings

log = logging.getLogger(__name__)


class EngineLike(Protocol):
    """The part of ``_morse_core.MorseEngine`` the controller uses."""

    trigger: str
    block_input: bool
    active: bool
    block_shortcut: str

    def start(self, callback: Callable[[str, str], None], use_hook: bool = True) -> None: ...
    def stop(self) -> None: ...
    def configure(self, dot_threshold_ms: int, letter_gap_ms: int, word_gap_ms: int = 0) -> None: ...
    def reset(self) -> None: ...


class InjectorLike(Protocol):
    def inject_text(self, text: str) -> None: ...
    def inject_backspaces(self, count: int) -> None: ...


class ControllerListener(Protocol):
    """Optional UI feedback. These are called on the worker thread."""

    def on_symbol(self, symbol: str) -> None: ...
    def on_letter(self, sequence: str, text: str | None) -> None: ...
    def on_error(self, message: str) -> None: ...


class NativeInjector:
    """Adapter for the Rust ``inject_text`` / ``inject_backspaces`` functions."""

    def __init__(self, native_module) -> None:
        self._native = native_module

    def inject_text(self, text: str) -> None:
        self._native.inject_text(text)

    def inject_backspaces(self, count: int) -> None:
        self._native.inject_backspaces(count)


class MorseController:
    HISTORY_LIMIT = 500

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
            self._engine.trigger = s.trigger
            self._apply_blocking(s)
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
            if new.trigger != old.trigger:
                self._engine.trigger = new.trigger
            self._apply_blocking(new)
            self._settings = new

    def _apply_blocking(self, s: Settings) -> None:
        self._engine.block_shortcut = s.block_shortcut
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
        if emission.erase:
            self._injector.inject_backspaces(emission.erase)
            for _ in range(min(emission.erase, len(self.history))):
                self.history.pop()
        if emission.text:
            self._injector.inject_text(emission.text)
            self.history.extend(emission.text)

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
