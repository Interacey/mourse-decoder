"""Controller side of the calibration: timing events reach the listener and nothing is typed while measuring."""

from __future__ import annotations

import time

import pytest
from mourse_decoder._native import NATIVE_AVAILABLE
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.settings import Settings


class TimingListener:
    def __init__(self) -> None:
        self.timings: list[tuple[str, int]] = []
        self.letters: list[tuple[str, str | None]] = []

    def on_symbol(self, symbol: str) -> None: ...

    def on_letter(self, sequence: str, text: str | None) -> None:
        self.letters.append((sequence, text))

    def on_error(self, message: str) -> None: ...

    def on_timing(self, kind: str, ms: int) -> None:
        self.timings.append((kind, ms))


def make(engine, injector, listener=None):
    controller = MorseController(engine, injector, DictionaryManager(), Settings(), listener=listener)
    controller.start()
    return controller


def test_timing_events_reach_the_listener_only_while_calibrating(fake_engine, injector):
    listener = TimingListener()
    controller = make(fake_engine, injector, listener)
    fake_engine.emit("press_ms", "120")
    assert listener.timings == []
    controller.set_calibrating(True)
    fake_engine.emit("press_ms", "120")
    fake_engine.emit("gap_ms", "640")
    assert listener.timings == [("press_ms", 120), ("gap_ms", 640)]
    controller.set_calibrating(False)
    fake_engine.emit("gap_ms", "700")
    assert len(listener.timings) == 2


def test_nothing_is_typed_while_calibrating_and_typing_resumes_after(fake_engine, injector):
    listener = TimingListener()
    controller = make(fake_engine, injector, listener)
    controller.set_calibrating(True)
    fake_engine.letters("...", "---")
    fake_engine.emit("word_gap")
    assert injector.calls == [] and listener.letters == []
    assert controller.settings.enabled  # the setting itself is untouched
    controller.set_calibrating(False)
    fake_engine.letters("...")
    assert injector.buffer == "S"


def test_switching_resets_the_engine_once_per_change(fake_engine, injector):
    controller = make(fake_engine, injector)
    before = fake_engine.reset_calls
    controller.set_calibrating(True)
    controller.set_calibrating(True)
    controller.set_calibrating(False)
    assert fake_engine.reset_calls == before + 2
    assert not controller.calibrating


def test_a_listener_without_on_timing_and_bad_payloads_are_harmless(fake_engine, injector, listener):
    controller = make(fake_engine, injector, listener)  # the shared RecordingListener has no on_timing
    controller.set_calibrating(True)
    fake_engine.emit("press_ms", "100")
    fake_engine.emit("press_ms", "abc")
    timing = TimingListener()
    controller.set_listener(timing)
    fake_engine.emit("gap_ms", "abc")
    assert timing.timings == []
    assert listener.errors == []


def test_timing_events_do_not_disturb_normal_typing(fake_engine, injector, listener):
    make(fake_engine, injector, listener)
    fake_engine.emit("press_ms", "50")
    fake_engine.emit("gap_ms", "300")
    fake_engine.letters("...")
    assert injector.buffer == "S"


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust-Modul nicht gebaut (maturin develop)")
def test_the_real_engine_reports_the_press_before_the_symbol_and_the_gap_at_the_next_press():
    from mourse_decoder import _morse_core as core

    events: list[tuple[str, str]] = []
    engine = core.MorseEngine(dot_threshold_ms=150, letter_gap_ms=400, word_gap_ms=0)
    engine.start(lambda kind, payload: events.append((kind, payload)), use_hook=False)
    try:
        engine.feed_press()
        time.sleep(0.05)
        engine.feed_release()
        time.sleep(0.1)
        engine.feed_press()
        time.sleep(0.25)
        engine.feed_release()
        time.sleep(0.8)
    finally:
        engine.stop()
    kinds = [kind for kind, _ in events]
    assert kinds == ["press_ms", "symbol", "gap_ms", "press_ms", "symbol", "letter"]
    values = {i: int(p) for i, (k, p) in enumerate(events) if k in ("press_ms", "gap_ms")}
    assert 40 <= values[0] <= 150 and 90 <= values[2] <= 250 and 240 <= values[3] <= 400
    assert [p for k, p in events if k == "symbol"] == [".", "-"]
