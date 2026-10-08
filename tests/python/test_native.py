"""Integration tests against the real Rust module (through PyO3).

Skipped until ``maturin develop`` has run. The global hooks and real typing are not
tested (they need a display and would type into the terminal); the tests use
``use_hook=False`` with ``feed_press``/``feed_release`` instead.
"""

from __future__ import annotations

import threading
import time

import pytest
from mourse_decoder._native import NATIVE_AVAILABLE
from mourse_decoder.settings import TRIGGER_NAMES

pytestmark = pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust-Modul nicht gebaut (maturin develop)")

if NATIVE_AVAILABLE:
    from mourse_decoder import _morse_core as core


class EventCollector:
    """Collects callbacks thread-safely and can wait for a given kind."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self._cond = threading.Condition()

    def __call__(self, kind: str, payload: str) -> None:
        with self._cond:
            self.events.append((kind, payload))
            self._cond.notify_all()

    def wait_for(self, kind: str, count: int = 1, timeout: float = 3.0) -> list[str]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                found = [p for k, p in self.events if k == kind]
                if len(found) >= count:
                    return found
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"Timeout beim Warten auf {kind}: {self.events}")
                self._cond.wait(remaining)


def key(engine, hold_s: float, pause_s: float = 0.03) -> None:
    engine.feed_press()
    time.sleep(hold_s)
    engine.feed_release()
    time.sleep(pause_s)


@pytest.fixture
def engine():
    # generous threshold (150 ms) so scheduling jitter in CI doesn't matter
    eng = core.MorseEngine(dot_threshold_ms=150, letter_gap_ms=250, word_gap_ms=0)
    yield eng
    eng.stop()


def test_trigger_lists_are_in_sync():
    # the Python list (settings.py) must match the Rust list exactly
    assert tuple(core.available_triggers()) == TRIGGER_NAMES


@pytest.mark.parametrize(("ms", "expected"), [(0, "."), (199.999, "."), (200, "-"), (1000, "-")])
def test_classify_duration(ms, expected):
    assert core.classify_duration(ms, 200) == expected


def test_classify_duration_rejects_invalid_input():
    with pytest.raises(ValueError):
        core.classify_duration(-1, 200)
    with pytest.raises(ValueError):
        core.classify_duration(10, 0)


def test_constructor_validation():
    with pytest.raises(ValueError, match="word_gap_ms"):
        core.MorseEngine(letter_gap_ms=600, word_gap_ms=500)
    with pytest.raises(ValueError, match="Unbekannter Taster"):
        core.MorseEngine(trigger="Hyper")


def test_properties_and_repr():
    eng = core.MorseEngine(dot_threshold_ms=180, letter_gap_ms=500, word_gap_ms=1200, trigger="f8")
    assert (eng.dot_threshold_ms, eng.letter_gap_ms, eng.word_gap_ms) == (180, 500, 1200)
    assert eng.trigger == "F8"  # normalized
    assert "running=false" in repr(eng)
    eng.trigger = "MouseRight"
    assert eng.trigger == "MouseRight"
    with pytest.raises(ValueError):
        eng.trigger = "Hyper"


def test_letter_a_end_to_end(engine):
    events = EventCollector()
    engine.start(events, use_hook=False)
    assert engine.is_running
    key(engine, 0.02)  # dot
    key(engine, 0.30)  # dash
    assert events.wait_for("letter") == [".-"]
    assert [p for k, p in events.events if k == "symbol"] == [".", "-"]


def test_word_gap_event():
    eng = core.MorseEngine(dot_threshold_ms=150, letter_gap_ms=150, word_gap_ms=400)
    events = EventCollector()
    eng.start(events, use_hook=False)
    try:
        key(eng, 0.02)
        events.wait_for("word_gap")
        kinds = [k for k, _ in events.events]
        assert kinds == ["symbol", "letter", "word_gap"]
    finally:
        eng.stop()


def test_configure_while_running(engine):
    events = EventCollector()
    engine.start(events, use_hook=False)
    engine.configure(1000, 250, 0)  # now even 300 ms is a dot
    time.sleep(0.05)  # let the configure message be processed before the press
    key(engine, 0.30)
    assert events.wait_for("letter") == ["."]


def test_reset_discards_pending_sequence(engine):
    events = EventCollector()
    engine.start(events, use_hook=False)
    engine.configure(150, 2000, 0)  # long pause, so the reset surely comes first
    key(engine, 0.02)
    engine.reset()
    time.sleep(0.1)
    assert [k for k, _ in events.events] == ["symbol"]


def test_start_twice_fails(engine):
    engine.start(lambda *_: None, use_hook=False)
    with pytest.raises(RuntimeError, match="läuft bereits"):
        engine.start(lambda *_: None, use_hook=False)


def test_feed_without_start_fails(engine):
    with pytest.raises(RuntimeError, match="nicht gestartet"):
        engine.feed_press()


def test_stop_is_idempotent_and_restart_works(engine):
    events = EventCollector()
    engine.start(events, use_hook=False)
    engine.stop()
    engine.stop()
    assert not engine.is_running
    engine.start(events, use_hook=False)
    key(engine, 0.02)
    assert events.wait_for("letter") == ["."]


def test_callback_exception_does_not_kill_worker(engine):
    calls: list[str] = []
    errors: list[object] = []
    import sys

    def callback(kind, payload):
        calls.append(kind)
        if len(calls) == 1:
            raise ValueError("absichtlicher Fehler")

    old_hook = sys.unraisablehook
    sys.unraisablehook = errors.append  # write_unraisable ends up here
    try:
        engine.start(callback, use_hook=False)
        key(engine, 0.02)
        deadline = time.monotonic() + 3
        while "letter" not in calls and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        sys.unraisablehook = old_hook
    assert calls[:2] == ["symbol", "letter"]  # the worker survives the error
    assert len(errors) == 1


def test_stop_from_inside_slow_callback_does_not_deadlock(engine):
    # regression test for releasing the GIL in stop(): the worker waits in the
    # callback while the main thread calls stop()
    entered = threading.Event()

    def slow_callback(kind, payload):
        entered.set()
        time.sleep(0.2)

    engine.start(slow_callback, use_hook=False)
    key(engine, 0.02, pause_s=0)
    assert entered.wait(2)
    started = time.monotonic()
    engine.stop()
    assert time.monotonic() - started < 2


def test_native_controller_round_trip():
    # the whole Python stack with the real Rust engine, only the injector is a fake
    from conftest import RecordingInjector
    from mourse_decoder.controller import MorseController
    from mourse_decoder.dictionary import DictionaryManager
    from mourse_decoder.settings import Settings

    eng = core.MorseEngine()
    injector = RecordingInjector()
    settings = Settings(dot_threshold_ms=150, letter_gap_ms=250, word_gap_ms=0, alphabet="greek")
    controller = MorseController(eng, injector, DictionaryManager(), settings, use_hook=False)
    controller.start()
    try:
        for _ in range(3):
            key(eng, 0.30)  # "---" = Ο (omicron)
        deadline = time.monotonic() + 3
        while not injector.buffer and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        controller.stop()
    assert injector.buffer == "Ο"


def test_any_key_and_extra_mouse_buttons_are_accepted_as_trigger():
    eng = core.MorseEngine(trigger="Mouse5")
    assert eng.trigger == "Mouse5"
    eng.trigger = "vk65"
    assert eng.trigger == "VK65"
    for bad in ("Mouse3", "VK0", "VK256"):
        with pytest.raises(ValueError, match="Unbekannter Taster"):
            eng.trigger = bad


def test_capture_api_is_idle_without_input():
    # real input can't be produced in a test, so only check idle and cancel
    core.begin_capture()
    assert core.poll_capture() is None
    core.cancel_capture()
    assert core.poll_capture() is None

def test_blocking_api_on_the_real_module():
    eng = core.MorseEngine()
    eng.block_input = True
    assert eng.block_input is True
    eng.block_input = False
    assert eng.active is True
    eng.active = False
    assert eng.active is False
    eng.active = True
    eng.block_shortcut = "Ctrl+Alt+F9"
    eng.block_shortcut = ""
    with pytest.raises(ValueError):
        eng.block_shortcut = "Hyper+F9"
