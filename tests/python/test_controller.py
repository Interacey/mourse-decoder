"""Tests for the controller (engine, decoder, injector) with fakes."""

from __future__ import annotations

import pytest
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import AlphabetError, DictionaryManager
from mourse_decoder.settings import Settings


def make_controller(engine, injector, listener=None, **settings_changes):
    settings = Settings().replace(**settings_changes)
    controller = MorseController(engine, injector, DictionaryManager(), settings, listener=listener)
    controller.start()
    return controller


def test_start_configures_and_starts_engine(fake_engine, injector):
    make_controller(fake_engine, injector, dot_threshold_ms=150, trigger="F8")
    assert fake_engine.started
    assert fake_engine.configure_calls == [(150, 600, 1400)]
    assert fake_engine.trigger == "F8"
    assert fake_engine.use_hook is True


def test_start_is_idempotent(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.start()  # the fake engine would raise on a second real start
    assert controller.running


def test_letters_are_typed(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    fake_engine.letters("...", "---", "...")
    fake_engine.emit("word_gap")
    assert injector.buffer == "SOS "
    assert controller.typed_text == "SOS "


def test_listener_gets_symbols_and_letters(fake_engine, injector, listener):
    make_controller(fake_engine, injector, listener=listener)
    fake_engine.letters(".-")
    assert listener.symbols == [".", "-"]
    assert listener.letters == [(".-", "A")]


def test_unknown_sequence_types_nothing_but_notifies(fake_engine, injector, listener):
    make_controller(fake_engine, injector, listener=listener)
    fake_engine.letters("........")
    assert injector.calls == []
    assert listener.letters == [("........", None)]


def test_disabled_output_is_suppressed(fake_engine, injector):
    make_controller(fake_engine, injector, enabled=False)
    fake_engine.letters(".-")
    fake_engine.emit("word_gap")
    assert injector.calls == []


def test_wabun_dakuten_uses_backspace(fake_engine, injector):
    make_controller(fake_engine, injector, alphabet="wabun")
    fake_engine.letters(".-..", "..")  # カ, ゛
    assert injector.calls == [("text", "カ"), ("backspace", 1), ("text", "ガ")]
    assert injector.buffer == "ガ"


def test_apply_settings_switches_alphabet_and_resets_engine(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_settings(controller.settings.replace(alphabet="cyrillic"))
    assert fake_engine.reset_calls == 1
    fake_engine.letters(".--")
    assert injector.buffer == "В"  # Cyrillic Ve, not Latin W


def test_apply_settings_only_reconfigures_on_timing_change(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_settings(controller.settings.replace(enabled=False))
    assert len(fake_engine.configure_calls) == 1  # only the start
    controller.apply_settings(controller.settings.replace(letter_gap_ms=800, word_gap_ms=2000))
    assert fake_engine.configure_calls[-1] == (200, 800, 2000)


def test_apply_settings_changes_trigger(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_settings(controller.settings.replace(trigger="MouseMiddle"))
    assert fake_engine.trigger == "MouseMiddle"


def test_invalid_settings_are_rejected_without_side_effects(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    before = controller.settings
    with pytest.raises(ValueError):
        controller.apply_settings(before.replace(dot_threshold_ms=1))
    assert controller.settings is before


def test_unknown_alphabet_keeps_old_decoder(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    with pytest.raises(AlphabetError):
        controller.apply_settings(controller.settings.replace(alphabet="klingonisch"))
    assert controller.settings.alphabet == "latin"
    assert fake_engine.reset_calls == 0
    fake_engine.letters(".-")
    assert injector.buffer == "A"


def test_invalid_initial_alphabet_fails_fast(fake_engine, injector):
    with pytest.raises(AlphabetError):
        MorseController(fake_engine, injector, DictionaryManager(), Settings(alphabet="gibtsnicht"))


def test_injector_error_is_reported_not_raised(fake_engine, injector, listener):
    make_controller(fake_engine, injector, listener=listener)
    injector.fail = True
    fake_engine.letters(".-")  # must NOT leak into the (Rust) worker
    assert listener.errors == ["Injektion kaputt"]


def test_listener_error_does_not_stop_typing(fake_engine, injector):
    class BrokenListener:
        def on_symbol(self, symbol):
            raise RuntimeError("UI kaputt")

        on_letter = on_error = on_symbol

    make_controller(fake_engine, injector, listener=BrokenListener())
    fake_engine.letters(".-")
    assert injector.buffer == "A"


def test_unknown_event_kind_is_ignored(fake_engine, injector):
    make_controller(fake_engine, injector)
    fake_engine.emit("future_event", "x")
    assert injector.calls == []


def test_stop(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.stop()
    controller.stop()  # idempotent
    assert not fake_engine.started
    assert not controller.running


def test_history_is_bounded(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    for _ in range(MorseController.HISTORY_LIMIT + 50):
        fake_engine.letters(".")
    assert len(controller.typed_text) == MorseController.HISTORY_LIMIT


def test_telecode_end_to_end(fake_engine, injector, telecode_db):
    settings = Settings(alphabet=DictionaryManager.TELECODE_ID)
    controller = MorseController(fake_engine, injector, DictionaryManager(telecode_db=telecode_db), settings)
    controller.start()
    # 0 0 2 2 in Morse
    fake_engine.letters("-----", "-----", "..---", "..---")
    assert injector.buffer == "中"
    assert controller.typed_text == "中"
