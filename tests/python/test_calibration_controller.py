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
    engine.set_measuring(True)
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
    assert events[0] == ("measuring", "1")
    events = events[1:]
    kinds = [kind for kind, _ in events]
    assert kinds == ["press_ms", "symbol", "gap_ms", "press_ms", "symbol", "letter"]
    values = {i: int(p) for i, (k, p) in enumerate(events) if k in ("press_ms", "gap_ms")}
    assert 40 <= values[0] <= 150 and 90 <= values[2] <= 250 and 240 <= values[3] <= 400
    assert [p for k, p in events if k == "symbol"] == [".", "-"]


# --- review fixes: measuring switch, lifecycle of the flag, watchdog ------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class EndListener(TimingListener):
    def __init__(self) -> None:
        super().__init__()
        self.ended: list[str] = []

    def on_calibration_ended(self, reason: str) -> None:
        self.ended.append(reason)


def make_with_clock(engine, injector, listener=None, **settings):
    clock = Clock()
    controller = MorseController(
        engine, injector, DictionaryManager(), Settings().replace(**settings), listener=listener, clock=clock
    )
    controller.start()
    return controller, clock


def test_the_engine_measures_only_while_calibrating(fake_engine, injector):
    controller = make(fake_engine, injector)
    assert fake_engine.measuring is False and fake_engine.measuring_calls == []  # nothing until asked
    assert controller.set_calibrating(True) is True
    assert fake_engine.measuring is True
    assert controller.set_calibrating(False) is False
    assert fake_engine.measuring is False
    assert fake_engine.measuring_calls == [True, False]


def test_measuring_cannot_start_on_a_stopped_controller(fake_engine, injector):
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings())
    assert controller.set_calibrating(True) is False
    assert not controller.calibrating and fake_engine.measuring_calls == []
    controller.start()
    controller.stop()
    assert controller.set_calibrating(True) is False


def test_stop_ends_measuring_and_tells_the_page(fake_engine, injector):
    listener = EndListener()
    controller = make(fake_engine, injector, listener)
    controller.set_calibrating(True)
    controller.stop()
    assert not controller.calibrating and fake_engine.measuring is False
    assert listener.ended == ["stopped"]
    controller.start()
    assert not controller.calibrating
    fake_engine.letters("...")
    assert injector.buffer == "S"  # not muted after stop and start


def test_stop_without_measuring_tells_nobody(fake_engine, injector):
    listener = EndListener()
    controller = make(fake_engine, injector, listener)
    controller.stop()
    assert listener.ended == [] and fake_engine.measuring_calls == []


def test_a_listener_without_on_calibration_ended_is_fine(fake_engine, injector, listener):
    controller = make(fake_engine, injector, listener)  # RecordingListener has no such method
    controller.set_calibrating(True)
    controller.stop()
    assert not controller.calibrating and listener.errors == []


def test_a_failing_listener_does_not_keep_measuring_on(fake_engine, injector):
    class Broken(TimingListener):
        def on_calibration_ended(self, reason: str) -> None:
            raise RuntimeError("page is gone")

    controller = make(fake_engine, injector, Broken())
    controller.set_calibrating(True)
    controller.stop()
    assert not controller.calibrating and fake_engine.measuring is False


def test_measuring_ends_by_itself_after_the_time_limit(fake_engine, injector):
    listener = EndListener()
    controller, clock = make_with_clock(fake_engine, injector, listener)
    controller.set_calibrating(True)
    clock.now += MorseController.CALIBRATION_MAX_S - 1
    assert controller.check_calibration_timeout() is False and controller.calibrating
    clock.now += 1
    assert controller.check_calibration_timeout() is True
    assert not controller.calibrating and fake_engine.measuring is False
    assert listener.ended == ["timeout"]
    assert controller.check_calibration_timeout() is False  # once


def test_the_time_limit_counts_from_the_latest_start(fake_engine, injector):
    controller, clock = make_with_clock(fake_engine, injector)
    controller.set_calibrating(True)
    clock.now += MorseController.CALIBRATION_MAX_S - 5
    controller.set_calibrating(False)
    controller.set_calibrating(True)
    clock.now += 10
    assert controller.check_calibration_timeout() is False and controller.calibrating


def test_an_engine_event_after_the_time_limit_ends_the_muting_and_is_typed(fake_engine, injector):
    # nobody polls and the page forgot to stop: the next letter must not vanish
    listener = EndListener()
    controller, clock = make_with_clock(fake_engine, injector, listener)
    controller.set_calibrating(True)
    fake_engine.letters("...")
    assert injector.buffer == ""
    clock.now += MorseController.CALIBRATION_MAX_S
    fake_engine.letters("...")
    assert injector.buffer == "S" and listener.ended == ["timeout"]


def test_the_time_limit_is_ten_minutes():
    assert MorseController.CALIBRATION_MAX_S == 600


def test_switching_on_drops_a_letter_keyed_before_and_off_drops_one_keyed_while_measuring(fake_engine, injector):
    controller = make(fake_engine, injector)
    controller.set_calibrating(True)
    assert fake_engine.reset_calls >= 1
    controller.set_calibrating(False)
    assert fake_engine.reset_calls >= 2  # a letter started while measuring is not typed afterwards


def _real_controller(injector, **settings):
    from mourse_decoder import _morse_core as core

    engine = core.MorseEngine()
    timing = {"dot_threshold_ms": 150, "letter_gap_ms": 300, "word_gap_ms": 0}  # the controller configures the engine
    controller = MorseController(
        engine, injector, DictionaryManager(), Settings().replace(**timing, **settings), use_hook=False
    )
    controller.start()
    return engine, controller


def _tap(engine, hold_s=0.02, pause_s=0.03):
    engine.feed_press()
    time.sleep(hold_s)
    engine.feed_release()
    time.sleep(pause_s)


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust-Modul nicht gebaut (maturin develop)")
def test_real_engine_a_letter_started_while_measuring_is_not_typed_after_switching_off(injector):
    engine, controller = _real_controller(injector)
    try:
        controller.set_calibrating(True)
        _tap(engine)
        _tap(engine)
        controller.set_calibrating(False)  # the open letter ".." is discarded by the reset
        time.sleep(0.6)
        assert injector.buffer == ""
        _tap(engine)  # normal typing works again: "."
        _tap(engine)
        time.sleep(0.6)
        assert injector.buffer == "I"
    finally:
        controller.stop()


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust-Modul nicht gebaut (maturin develop)")
def test_real_engine_a_letter_keyed_before_measuring_is_dropped_by_the_switch_on(injector):
    engine, controller = _real_controller(injector)
    try:
        _tap(engine)
        controller.set_calibrating(True)
        controller.set_calibrating(False)
        time.sleep(0.6)
        assert injector.buffer == ""  # intended: the half-keyed letter belongs to neither mode
        _tap(engine)
        time.sleep(0.6)
        assert injector.buffer == "E"
    finally:
        controller.stop()


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust-Modul nicht gebaut (maturin develop)")
def test_real_engine_sends_no_timing_events_unless_calibrating_and_stop_switches_off(injector):
    engine, controller = _real_controller(injector)
    listener = TimingListener()
    controller.set_listener(listener)
    kinds: list[str] = []
    try:
        assert engine.measuring is False
        _tap(engine)
        _tap(engine)
        time.sleep(0.5)
        assert listener.timings == [] and injector.buffer == "I"
        controller.set_calibrating(True)
        assert engine.measuring is True
        _tap(engine)
        _tap(engine)
        time.sleep(0.1)
        kinds = [k for k, _ in listener.timings]
        assert kinds == ["press_ms", "gap_ms", "press_ms"]
        assert injector.buffer == "I"  # muted while measuring
    finally:
        controller.stop()
    assert engine.measuring is False


def test_a_letter_of_the_measuring_phase_is_not_typed_when_it_arrives_after_switching_off(fake_engine, injector):
    # the engine's "measuring" marker 0 is still on its way (a lagging worker) when the flag is cleared:
    # the letter in front of it was keyed while measuring and has to stay silent
    controller = make(fake_engine, injector)
    controller.set_calibrating(True)
    fake_engine.hold_acks = True
    controller.set_calibrating(False)
    assert not controller.calibrating
    fake_engine.letters("...")
    fake_engine.emit("word_gap")
    assert injector.buffer == ""
    fake_engine.release_acks()
    fake_engine.letters("...")
    assert injector.buffer == "S"


def test_a_letter_keyed_before_switching_on_is_still_typed(fake_engine, injector):
    controller = make(fake_engine, injector)
    fake_engine.hold_acks = True
    controller.set_calibrating(True)
    fake_engine.letters("...")  # in front of marker 1
    assert injector.buffer == "S"
    fake_engine.release_acks()
    fake_engine.letters("...")
    assert injector.buffer == "S"  # muted from the marker on


def test_stop_and_start_clear_the_muting_even_without_a_marker(fake_engine, injector):
    controller = make(fake_engine, injector)
    controller.set_calibrating(True)
    fake_engine.hold_acks = True
    controller.stop()
    fake_engine.pending_acks.clear()  # the stopped worker never delivers the marker
    controller.start()
    fake_engine.letters("...")
    assert injector.buffer == "S"


# --- second review round ----------------------------------------------------------------------------


def _wabun(fake_engine, injector):
    return MorseController(fake_engine, injector, DictionaryManager(), Settings().replace(alphabet="wabun"))


@pytest.mark.parametrize("measure_between", [False, True])
def test_a_dakuten_after_a_measurement_does_not_turn_the_earlier_kana_into_a_voiced_one(
    fake_engine, injector, measure_between
):
    controller = _wabun(fake_engine, injector)
    controller.start()
    fake_engine.letters(".-..")  # a kana that a dakuten would voice
    typed = list(injector.calls)
    if measure_between:
        controller.set_calibrating(True)
        fake_engine.emit("word_gap")  # the long pause of the measurement, muted
        controller.set_calibrating(False)
    fake_engine.letters("..")  # dakuten keyed first after the pause
    new_calls = injector.calls[len(typed) :]
    if measure_between:
        assert ("backspace", 1) not in new_calls  # nothing to voice any more: it stands alone
    else:
        assert ("backspace", 1) in new_calls  # without measuring the combination works as before


def test_half_entered_telecode_digits_do_not_cross_a_measurement(fake_engine, injector):
    from mourse_decoder.decoders import TelecodeDecoder

    class Store:
        def lookup(self, code):
            return "X" if code == "1234" else None

        def close(self): ...

    digits = {".----": "1", "..---": "2", "...--": "3", "....-": "4"}
    outcomes = {}
    for measure_between in (False, True):
        engine, inj = type(fake_engine)(), type(injector)()
        controller = MorseController(
            engine, inj, DictionaryManager(), Settings(), decoder_factory=lambda _m, _a: TelecodeDecoder(Store(), digits)
        )
        controller.start()
        engine.letters(".----", "..---")
        if measure_between:
            controller.set_calibrating(True)
            controller.set_calibrating(False)
        engine.letters("...--", "....-")
        outcomes[measure_between] = inj.calls
    assert ("backspace", 3) in outcomes[False]  # 1, 2, 3, 4 is the code 1234
    assert ("backspace", 3) not in outcomes[True]  # the measurement cut the entry in two


def test_the_decoder_is_reset_at_both_markers(fake_engine, injector):
    controller = _wabun(fake_engine, injector)
    controller.start()
    resets = []
    controller._decoder.reset = lambda: resets.append(1)
    controller.set_calibrating(True)
    controller.set_calibrating(False)
    assert len(resets) == 2


def test_switching_sends_no_separate_reset_to_the_engine(fake_engine, injector):
    # the worker resets itself in the same step as the switch; a second message from here would leave a gap
    controller = make(fake_engine, injector)

    def boom():
        raise AssertionError("engine.reset() called")

    fake_engine.reset = boom
    controller.set_calibrating(True)
    controller.set_calibrating(False)
    controller._end_calibrating("timeout")


def test_a_letter_in_flight_while_stop_joins_the_worker_is_not_typed(fake_engine, injector):
    class Lagging(type(fake_engine)):
        def stop(self):
            self.emit("letter", "...")  # the worker finishes a letter of the measuring phase while being joined
            super().stop()

    engine = Lagging()
    controller = MorseController(engine, injector, DictionaryManager(), Settings())
    controller.start()
    controller.set_calibrating(True)
    controller.stop()
    assert injector.buffer == "" and injector.calls == []
    controller.start()
    engine.letters("...")
    assert injector.buffer == "S"  # the muting ended with the stop


def test_no_backspaces_are_sent_for_a_letter_that_arrives_while_the_program_stops(fake_engine, injector):
    from mourse_decoder.decoders import Emission

    controller = make(fake_engine, injector)
    controller._stopping = True
    with pytest.raises(Exception, match="beendet"):
        controller._emit_steps(Emission("x", erase=2))
    assert ("backspace", 2) not in injector.calls
