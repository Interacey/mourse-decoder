"""Scroll macros in the controller: wiring to the scroller, Esc handling and the macro lock."""

from __future__ import annotations

import json

from mourse_decoder import app
from mourse_decoder import controller as controller_module
from mourse_decoder.controller import MorseController, NativeInjector
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.function_keys import FunctionKeys
from mourse_decoder.scroller import DOWN, UP, Scroller
from mourse_decoder.settings import Settings

CODE = "--..--.."


class RecordingScroller:
    """Records what the controller asks for; has no thread."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, int | None]] = []
        self.running = False
        self.level = 0

    def request(self, direction: int) -> None:
        self.calls.append(("request", direction))

    def stop(self) -> bool:
        self.calls.append(("stop", None))
        was, self.running = self.running, False
        return was


def make_controller(tmp_path, engine, injector, codes, listener=None, scroller_factory=Scroller):
    (tmp_path / "macros.json").write_text(json.dumps({"id": "macros", "codes": codes}), encoding="utf-8")
    controller = MorseController(
        engine,
        injector,
        DictionaryManager(smiley_directory=tmp_path),
        Settings().replace(smileys="macros"),
        listener=listener,
        scroller_factory=scroller_factory,
    )
    controller.start()
    return controller


def no_thread(**kwargs) -> Scroller:
    return Scroller(**kwargs, spawn=lambda body: None)


def test_scroll_code_requests_down_and_types_nothing(tmp_path, fake_engine, injector, listener):
    fake = None

    def factory(**kwargs):
        nonlocal fake
        fake = RecordingScroller(**kwargs)
        return fake

    make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, listener, factory)
    fake_engine.letters(CODE)
    assert fake.calls == [("request", DOWN)]
    assert injector.calls == []
    assert listener.letters == [(CODE, "{Scroll Down}")]


def test_the_same_code_three_times_speeds_up(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, scroller_factory=no_thread)
    fake_engine.letters(CODE, CODE, CODE)
    assert controller._scroller.level == 3 and controller._scroller.direction == DOWN


def test_scroll_up_and_stop(tmp_path, fake_engine, injector):
    fake = None

    def factory(**kwargs):
        nonlocal fake
        fake = RecordingScroller(**kwargs)
        return fake

    make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Up}", "--..-.--": "{Scroll Stop}"}, scroller_factory=factory)
    fake_engine.letters(CODE, "--..-.--")
    assert fake.calls == [("request", UP), ("stop", None)]


def test_esc_stops_scrolling(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, scroller_factory=no_thread)
    fake_engine.letters(CODE)
    assert controller._scroller.running
    fake_engine.abort_requested = True
    assert controller._scroll_stop_reason() == "Esc"
    assert not controller._scroller.running


def test_esc_taken_by_the_scroller_still_aborts_a_typing_macro(tmp_path, fake_engine, injector, listener, monkeypatch):
    controller = make_controller(
        tmp_path, fake_engine, injector, {CODE: "a{Warte 99999}b"}, listener, scroller_factory=no_thread
    )

    sleeps = 0

    def sleep_with_esc(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 5:
            raise AssertionError("macro was not aborted")
        fake_engine.abort_requested = True
        assert controller._take_esc()  # the scroller thread got past the lock check and reads the flag first

    monkeypatch.setattr(controller_module.time, "sleep", sleep_with_esc)
    fake_engine.letters(CODE)
    assert injector.buffer == "a" and "b" not in injector.buffer
    assert listener.errors == []
    assert not controller._macro_lock.locked()


def test_the_scroller_leaves_esc_to_a_typing_macro(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, scroller_factory=no_thread)
    fake_engine.abort_requested = True
    assert controller._macro_lock.acquire(blocking=False)
    try:
        assert controller._scroll_stop_reason() is None
        assert fake_engine.abort_requested is True
    finally:
        controller._macro_lock.release()
    assert controller._scroll_stop_reason() == "Esc"


def test_wheel_error_reaches_the_listener(tmp_path, fake_engine, injector, listener):
    def broken(notches: int) -> None:
        raise RuntimeError("kaputt")

    injector.scroll = broken
    controller = make_controller(
        tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, listener, scroller_factory=no_thread
    )
    fake_engine.letters(CODE)
    controller._scroller.run()
    assert listener.errors == ["Scrollen gestoppt: kaputt"]
    assert not controller._scroller.running


def test_stop_reason_after_stop_is_program_exit(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, scroller_factory=no_thread)
    controller.stop()
    assert controller._scroll_stop_reason() == "Programm wird beendet"


def test_a_stale_esc_does_not_prevent_scrolling(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, scroller_factory=no_thread)
    fake_engine.abort_requested = True
    fake_engine.letters(CODE)
    assert controller._scroller.running


def test_letters_still_type_while_scrolling(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, scroller_factory=no_thread)
    fake_engine.letters(CODE)
    assert controller._scroller.running
    fake_engine.letters(".-")
    assert injector.buffer == "A"
    assert not controller._macro_lock.locked()


def test_switching_morse_input_off_ends_scrolling(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, scroller_factory=no_thread)
    fake_engine.active = False
    assert controller._scroll_stop_reason() == "Morse-Eingabe ausgeschaltet"


def test_stop_stops_scrolling(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, scroller_factory=no_thread)
    fake_engine.letters(CODE)
    assert controller._scroller.running
    controller.stop()
    assert not controller._scroller.running


def test_function_key_slot_can_scroll(fake_engine, injector):
    fake = None

    def factory(**kwargs):
        nonlocal fake
        fake = RecordingScroller(**kwargs)
        return fake

    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings(), scroller_factory=factory)
    controller.start()
    keys = FunctionKeys()
    slot = keys.slot(13)
    controller.apply_function_keys(keys.replace_slot(slot.replace(trigger=slot.trigger or "Ctrl+F13", macro="{Scroll Down}")))
    assert controller.run_slot(13, block=True) is None
    assert fake.calls == [("request", DOWN)]


def test_a_scroll_code_drops_the_pending_word_gap(tmp_path, fake_engine, injector):
    make_controller(tmp_path, fake_engine, injector, {CODE: "{Scroll Down}"}, scroller_factory=no_thread)
    before = fake_engine.reset_calls
    fake_engine.letters(CODE)
    assert fake_engine.reset_calls > before


def test_native_injector_scrolls_through_the_native_module():
    class FakeNative:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def scroll_wheel(self, notches: int) -> None:
            self.calls.append(notches)

    native = FakeNative()
    NativeInjector(native).scroll(-1)
    assert native.calls == [-1]


def test_print_injector_shows_the_scroll(capsys):
    app._PrintInjector().scroll(2)
    assert capsys.readouterr().out == "[scrolle +2]\n"


def test_the_scroller_wheel_reaches_the_injector(fake_engine, injector):
    seen: dict = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return RecordingScroller(**kwargs)

    MorseController(fake_engine, injector, DictionaryManager(), Settings(), scroller_factory=factory)
    seen["wheel"](1)
    assert injector.calls == [("scroll", 1)]


def test_stop_reason_is_none_while_nothing_happens(fake_engine, injector):
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings(), scroller_factory=no_thread)
    assert controller._scroll_stop_reason() is None
