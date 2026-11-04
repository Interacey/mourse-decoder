"""Review round B: controller behaviour (dropped output, stop, chunked paste fallback, reload)."""

from __future__ import annotations

import threading
import time

import pytest
from mourse_decoder import clipboard
from mourse_decoder.controller import MorseController, NativeInjector
from mourse_decoder.decoders import Emission
from mourse_decoder.dictionary import AlphabetError, DictionaryManager
from mourse_decoder.settings import Settings


class FakeDecoder:
    def __init__(self, text="x"):
        self.text, self.resets = text, 0

    def decode(self, sequence):
        return Emission(self.text)

    def word_gap(self):
        return None

    def reset(self):
        self.resets += 1


def make(engine, injector, decoder):
    controller = MorseController(
        engine, injector, DictionaryManager(), Settings(), decoder_factory=lambda _d, _a: decoder
    )
    controller.start()
    return controller


def test_dropped_output_resets_the_decoder(fake_engine, injector):
    decoder = FakeDecoder()
    controller = make(fake_engine, injector, decoder)
    assert controller._macro_lock.acquire(blocking=False)  # a macro is typing
    try:
        fake_engine.letters(".-")
    finally:
        controller._macro_lock.release()
    assert injector.calls == []
    assert decoder.resets == 1


def test_typed_output_does_not_reset_the_decoder(fake_engine, injector):
    decoder = FakeDecoder()
    make(fake_engine, injector, decoder)
    fake_engine.letters(".-")
    assert injector.buffer == "x" and decoder.resets == 0


def test_aborted_output_resets_the_decoder(fake_engine, injector):
    decoder = FakeDecoder("{Wait 500}")
    make(fake_engine, injector, decoder)
    fake_engine.active = False  # switched off while the letter is typed
    fake_engine.letters(".-")
    assert decoder.resets == 1


def test_stop_ends_a_running_macro_quickly(fake_engine, injector):
    controller = make(fake_engine, injector, FakeDecoder("{Wait 60000}"))
    worker = threading.Thread(target=fake_engine.letters, args=(".-",), daemon=True)
    worker.start()
    time.sleep(0.2)
    assert worker.is_alive()
    controller.stop()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_start_after_stop_runs_macros_again(fake_engine, injector):
    controller = make(fake_engine, injector, FakeDecoder("ab"))
    controller.stop()
    controller.start()
    fake_engine.letters(".-")
    assert injector.buffer == "ab"


class PasteFallbackInjector:
    """pastes() is true, but the clipboard is busy: try_paste says False."""

    def __init__(self, engine):
        self.engine, self.typed = engine, []
        self.stop_after = None

    def pastes(self, text):
        return True

    def try_paste(self, text):
        return False

    def check_shortcut(self, combo):
        pass

    def inject_text(self, text):
        self.typed.append(text)
        if self.stop_after and len(self.typed) >= self.stop_after:
            self.engine.active = False


def test_busy_clipboard_fallback_is_typed_in_chunks(fake_engine):
    inj = PasteFallbackInjector(fake_engine)
    controller = make(fake_engine, inj, FakeDecoder("y" * 2500))
    fake_engine.letters(".-")
    assert len(inj.typed) == 25 and all(len(c) <= controller.TYPE_CHUNK for c in inj.typed)


def test_busy_clipboard_fallback_stops_when_switched_off(fake_engine):
    inj = PasteFallbackInjector(fake_engine)
    inj.stop_after = 2
    make(fake_engine, inj, FakeDecoder("y" * 2500))
    fake_engine.letters(".-")
    assert len(inj.typed) == 2


class Board:
    available = staticmethod(lambda: True)

    def __init__(self, busy):
        self.busy = busy

    def save(self):
        if self.busy:
            raise OSError("belegt")
        return []

    def restore(self, saved):
        pass

    def set_text(self, text):
        pass


class Native:
    def __init__(self):
        self.calls = []

    def inject_text(self, text):
        self.calls.append(("text", text))

    def press_shortcut(self, combo):
        self.calls.append(("keys", combo))


def test_try_paste_reports_a_busy_clipboard(monkeypatch):
    monkeypatch.setattr(NativeInjector, "PASTE_SETTLE_S", 0)
    native = Native()
    assert NativeInjector(native, Board(busy=True)).try_paste("x" * 2500) is False
    assert native.calls == []
    assert NativeInjector(native, Board(busy=False)).try_paste("x" * 2500) is True
    assert native.calls == [("keys", "Ctrl+V")]


def test_inject_text_still_types_when_the_clipboard_is_busy():
    native = Native()
    NativeInjector(native, Board(busy=True)).inject_text("x" * 2500)
    assert native.calls == [("text", "x" * 2500)]


def test_paste_settle_time_is_a_named_modest_constant():
    assert 0.3 < NativeInjector.PASTE_SETTLE_S <= 0.6


def test_failed_reload_keeps_the_old_decoder(fake_engine, injector):
    old = FakeDecoder("o")
    calls = {"n": 0}

    def factory(_d, _a):
        calls["n"] += 1
        if calls["n"] > 1:
            raise AlphabetError("kaputt")
        return old

    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings(), decoder_factory=factory)
    controller.start()
    with pytest.raises(AlphabetError):
        controller.reload_alphabet()
    fake_engine.letters(".-")
    assert injector.buffer == "o" and fake_engine.reset_calls == 0


class _Kernel:
    def __init__(self, pointer):
        self.pointer, self.freed = pointer, []

    def GlobalAlloc(self, flags, size):
        return 7

    def GlobalLock(self, handle):
        return self.pointer

    def GlobalUnlock(self, handle):
        pass

    def GlobalFree(self, handle):
        self.freed.append(handle)


@pytest.mark.skipif(not clipboard.available(), reason="Windows only")
def test_clipboard_put_raises_cleanly_when_lock_fails(monkeypatch):
    kernel = _Kernel(None)
    monkeypatch.setattr(clipboard, "_kernel32", kernel)
    with pytest.raises(OSError):
        clipboard._put(clipboard.CF_UNICODETEXT, b"ab")
    assert kernel.freed == [7]
