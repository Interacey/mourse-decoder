"""{Backspace 5}: a key with a repeat count."""

from __future__ import annotations

import pytest
from conftest import RecordingInjector
from mourse_decoder.controller import Emission
from mourse_decoder.macros import MAX_KEY_REPEAT, MacroError, parse
from test_function_keys import macro_keys, make_controller

check = RecordingInjector().check_shortcut


def test_a_count_repeats_the_key_step():
    assert parse("{Backspace 3}", check) == [("keys", "Backspace")] * 3
    assert parse("{space 18}", check) == [("keys", "space")] * 18


def test_count_works_with_modifiers_text_and_extra_spaces():
    assert parse("a{Ctrl+End  2 }b", check) == [("text", "a"), ("keys", "Ctrl+End"), ("keys", "Ctrl+End"), ("text", "b")]


def test_count_one_and_the_limit_are_allowed():
    assert parse("{Tab 1}", check) == [("keys", "Tab")]
    assert len(parse(f"{{Tab {MAX_KEY_REPEAT}}}", check)) == MAX_KEY_REPEAT


def test_count_takes_part_in_loops():
    assert parse("{Loop 2}{Tab 2}{EndLoop}", check) == [("keys", "Tab")] * 4


@pytest.mark.parametrize("macro", ["{Tab 0}", "{Tab 00}", f"{{Tab {MAX_KEY_REPEAT + 1}}}", "{Tab 99999999999999999999}"])
def test_count_out_of_range_is_refused(macro):
    with pytest.raises(MacroError, match=f"zwischen 1 und {MAX_KEY_REPEAT}") as info:
        parse(macro, check)
    assert "Nichts getippt" in str(info.value)


@pytest.mark.parametrize("macro", ["{Tab x}", "{Tab -3}", "{Tab 2.5}", "{Tab 2 3}", "{Crtl+C 2}"])
def test_a_bad_count_or_key_is_an_unknown_command(macro):
    with pytest.raises(MacroError, match="Unbekannter Befehl|zwischen 1 und"):
        parse(macro, check)


def test_wait_and_loop_keep_their_own_numbers():
    assert parse("{Wait 500}", check) == [("wait", 0.5)]
    assert parse("{Loop 2}a{EndLoop}", check) == [("text", "aa")]


def test_nested_repeats_are_still_limited():
    with pytest.raises(MacroError, match="zu viel"):
        parse("{Loop 1000}{Loop 1000}{Tab 1000}{EndLoop}{EndLoop}", check)


def test_the_controller_presses_the_key_that_often(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="ab{Backspace 4}c"))
    controller.run_slot(13, block=True)
    assert injector.calls == [("text", "ab")] + [("keys", "Backspace")] * 4 + [("text", "c")]


def test_a_bad_count_is_refused_when_the_slot_is_installed(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    with pytest.raises(ValueError, match="zwischen 1 und"):
        controller.apply_function_keys(macro_keys(F13="text{Backspace 0}"))
    assert injector.calls == []


def test_esc_stops_a_long_repeat_between_two_presses(fake_engine, injector, listener):
    controller = make_controller(fake_engine, injector, listener=listener)
    controller.apply_function_keys(macro_keys(F13="{Backspace 500}x"))
    presses = []

    def press(combo: str) -> None:
        presses.append(combo)
        if len(presses) == 7:
            fake_engine.abort_requested = True  # Esc

    injector.press_shortcut = press
    controller.run_slot(13, block=True)
    assert len(presses) == 7 and injector.buffer == ""
    assert listener.errors == []
    assert not controller._macro_lock.locked()


def test_morse_output_repeats_too(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller._decoder = type("D", (), {"decode": lambda s, q: Emission("{Backspace 2}"), "word_gap": lambda s: None})()
    fake_engine.letters(".-")
    assert injector.calls == [("keys", "Backspace")] * 2
