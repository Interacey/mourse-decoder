"""The {Delay N} and {Rollback} macro commands: parsing, order and timing, rolling back typed text."""

from __future__ import annotations

import json
import time

import pytest
from mourse_decoder import macros
from mourse_decoder.controller import MorseController, UndoResult
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.function_keys import FunctionKeys
from mourse_decoder.macros import MacroError
from mourse_decoder.scroller import Scroller
from mourse_decoder.settings import TYPING_DELAY_RANGE, Settings
from mourse_decoder.smileys import BUILTIN_SMILEY_DIR


def no_shortcut(combo):
    raise ValueError(combo)  # these commands must never be read as a key combination


def parse(text):
    return macros.parse(text, no_shortcut)




@pytest.mark.parametrize(
    "text",
    ["{Delay 100}", "{delay 100}", "{DELAY 100}", "{Delay100}", "{ Delay   100 }", "{Verzögerung 100}", "{verzögerung100}"],
)
def test_delay_spellings(text):
    assert parse(text) == [("delay", 100)]


@pytest.mark.parametrize("ms", [0, 1, 999, 10000])
def test_delay_accepts_the_whole_range(ms):
    assert parse(f"{{Delay {ms}}}") == [("delay", ms)]
    assert TYPING_DELAY_RANGE == (0, 10000), "{Delay} and typing_delay_ms share one range"


@pytest.mark.parametrize(
    "text",
    ["{Delay 10001}", "{Delay 99999}", "{Delay 123456}", "{Delay -5}", "{Delay abc}", "{Delay}", "{Delay 1.5}", "{Delay 100ms}", "{Delay ٣}"],
)
def test_bad_delays_refuse_the_macro_with_a_clear_message(text):
    with pytest.raises(MacroError, match=r"0 bis 10000 ms.*Nichts getippt"):
        parse("before" + text)


def test_a_delay_is_a_step_between_the_text_parts_not_a_wait():
    assert parse("ab{Delay 100}cd") == [("text", "ab"), ("delay", 100), ("text", "cd")]
    assert parse("{Delay 50}a{Delay 70}") == [("delay", 50), ("text", "a"), ("delay", 70)]
    assert parse("{Delay 250}") != parse("{Wait 250}")
    assert parse("{Wait 250}") == [("wait", 0.25)]


def test_an_escaped_delay_is_plain_text():
    assert parse(r"\{Delay 100}") == [("text", "{Delay 100}")]


def test_delays_in_a_loop_are_repeated():
    assert parse("{Loop 2 i}a{Delay 10}{EndLoop}") == [
        ("text", "a"), ("delay", 10), ("text", "a"), ("delay", 10)
    ]
    assert parse("{Loop 2 i}[i]{Delay 5}{EndLoop}") == [
        ("text", "0"), ("delay", 5), ("text", "1"), ("delay", 5)
    ]


def test_delay_and_wait_can_be_combined():
    assert parse("a{Delay 5}b{Wait 20}c") == [("text", "a"), ("delay", 5), ("text", "b"), ("wait", 0.02), ("text", "c")]


def test_delays_do_not_count_to_the_wait_limit():
    parse("{Loop 100}{Delay 10000}{EndLoop}")
    with pytest.raises(MacroError, match="Wartezeit"):
        parse("{Loop 61}{Wait 10000}{EndLoop}")


@pytest.mark.parametrize("text", ["{Rollback}", "{rollback}", "{ROLLBACK}", "{ Rollback }", "{Rückgängig}", "{rückgängig}"])
def test_rollback_spellings(text):
    assert parse(text) == [("rollback", None)]


def test_rollback_next_to_text():
    assert parse("{Rollback}Hello") == [("rollback", None), ("text", "Hello")]
    assert parse("ab{Rollback}") == [("text", "ab"), ("rollback", None)]


@pytest.mark.parametrize("text", ["{Rollbackx}", "{Rollback 3}", "{Rollbak}"])
def test_misspelled_rollback_is_an_unknown_command(text):
    with pytest.raises(MacroError, match="Unbekannter Befehl"):
        parse(text)


def test_an_escaped_rollback_is_plain_text():
    assert parse(r"\{Rollback}") == [("text", "{Rollback}")]


def test_the_import_warning_lists_rollback_but_not_delay_or_wait(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"id": "x", "codes": {"--..--..": "{Rollback}", "--..-.-.": "a{Delay 100}b{Wait 5}"}}), encoding="utf-8")
    assert macros.commands_in_file(path) == ["{Rollback}"]


def test_existing_macro_files_are_read_as_before():
    for path in BUILTIN_SMILEY_DIR.glob("*.json"):
        codes = json.loads(path.read_text(encoding="utf-8"))["codes"]
        for text in codes.values():
            kinds = {kind for kind, _ in parse(text)}
            assert kinds == {"text"}, (path.name, text)
    assert parse("{Wait 500}x{Clipboard}{Scroll Down}") == [
        ("wait", 0.5), ("text", "x"), ("clipboard", None), ("scroll", "down")
    ]
    assert parse("{^_^}") == [("text", "{^_^}")]


def test_a_slot_with_a_bad_delay_command_is_reported():
    keys = FunctionKeys()
    keys = keys.replace_slot(keys.slot(13).replace(macro="a{Delay 20000}"))
    errors = keys.validate(None, lambda combo: None)
    assert any(e.startswith("F13: ") and "0 bis 10000" in e for e in errors)




def make_controller(engine, injector, codes=None, tmp_path=None, **settings_changes) -> MorseController:
    manager = DictionaryManager()
    settings = Settings().replace(**settings_changes)
    if codes is not None:
        (tmp_path / "macros.json").write_text(json.dumps({"id": "macros", "codes": codes}), encoding="utf-8")
        manager = DictionaryManager(smiley_directory=tmp_path)
        settings = settings.replace(smileys="macros")
    controller = MorseController(
        engine, injector, manager, settings, scroller_factory=lambda **kwargs: Scroller(**kwargs, spawn=lambda body: None)
    )
    controller.start()
    return controller


def run_macro(controller, macro: str, number: int = 13) -> None:
    keys = controller.function_keys
    controller.apply_function_keys(keys.replace_slot(keys.slot(number).replace(macro=macro)))
    assert controller.run_slot(number, block=True) is None


def backspaces(injector) -> list[int]:
    return [count for kind, count in injector.calls if kind == "backspace"]


def test_delay_sets_the_pause_for_the_characters_after_it(fake_engine, injector):
    controller = make_controller(fake_engine, injector, typing_delay_ms=20)
    run_macro(controller, "a{Delay 100}bc{Delay 5}d")
    assert injector.typed_with == [("a", 20), ("bc", 100), ("d", 5)]
    assert injector.buffer == "abcd"


def test_delay_is_no_pause_by_itself(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller._sleep = lambda seconds: injector.calls.append(("sleep", seconds))
    run_macro(controller, "a{Delay 9000}b{Wait 100}c")
    assert injector.calls == [("text", "a"), ("text", "b"), ("sleep", 0.1), ("text", "c")]


def test_delay_zero_means_no_pause(fake_engine, injector):
    controller = make_controller(fake_engine, injector, typing_delay_ms=40)
    run_macro(controller, "a{Delay 0}b")
    assert injector.typed_with == [("a", 40), ("b", 0)]


def test_a_delay_in_a_loop_is_set_again_every_round(fake_engine, injector):
    controller = make_controller(fake_engine, injector, typing_delay_ms=20)
    run_macro(controller, "{Loop 2 i}{Delay 7}x{EndLoop}y")
    assert injector.typed_with == [("x", 7), ("xy", 7)], "the text after the last delay is typed in one go"


def test_a_later_delay_in_the_same_line_replaces_the_earlier_one(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Delay 10}a{Delay 30}b{Delay 10}c")
    assert [d for _, d in injector.typed_with] == [10, 30, 10]


def test_precedence_delay_beats_the_slot_beats_the_settings(fake_engine, injector):
    controller = make_controller(fake_engine, injector, typing_delay_ms=45)
    keys = controller.function_keys
    controller.apply_function_keys(keys.replace_slot(keys.slot(13).replace(macro="a{Delay 5}b", typing_delay_ms=120)))
    assert controller.run_slot(13, block=True) is None
    assert injector.typed_with == [("a", 120), ("b", 5)], "the slot over the settings, then the inline value over both"
    keys = controller.function_keys
    controller.apply_function_keys(keys.replace_slot(keys.slot(14).replace(macro="a{Delay 5}b")))
    controller.run_slot(14, block=True)
    assert injector.typed_with[2:] == [("a", 45), ("b", 5)], "without a slot value the settings apply first"
    controller.run_slot(13, block=True)
    assert injector.typed_with[4:] == [("a", 120), ("b", 5)]


def test_a_delay_does_not_leak_into_the_next_run(fake_engine, injector):
    controller = make_controller(fake_engine, injector, typing_delay_ms=45)
    run_macro(controller, "a{Delay 5}b", 13)
    run_macro(controller, "c", 14)
    fake_engine.letters(".")
    assert injector.typed_with == [("a", 45), ("b", 5), ("c", 45), ("E", 45)]


def test_a_delay_changes_nothing_for_the_undo_length(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "ab{Delay 5}c")
    assert controller.undo_last_output() == UndoResult(3)


def test_a_bad_delay_types_nothing(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    keys = controller.function_keys
    controller._function_keys = keys.replace_slot(keys.slot(13).replace(macro="abc{Delay 10001}"))
    reason = controller.run_slot(13, block=True)
    assert "0 bis 10000" in reason
    assert injector.calls == []


def test_a_wait_still_waits_and_stops_when_morse_input_is_switched_off(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    started = time.monotonic()
    run_macro(controller, "a{Wait 150}b")
    assert time.monotonic() - started >= 0.14
    controller._sleep = lambda seconds: setattr(fake_engine, "active", False) or controller._stop_if_switched_off()
    injector.buffer = ""
    run_macro(controller, "a{Wait 5000}b")
    assert injector.buffer == "a"


def test_rollback_deletes_what_the_clipboard_macro_typed(fake_engine, injector):
    injector.clipboard = "abc"
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Clipboard}", 13)
    assert injector.buffer == "abc"
    run_macro(controller, "{Rollback}", 14)
    assert injector.buffer == ""
    assert backspaces(injector) == [3]


def test_rollback_of_a_very_long_text_goes_in_pieces(fake_engine, injector):
    injector.clipboard = "x" * 25_000
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Clipboard}", 13)
    run_macro(controller, "{Rollback}", 14)
    assert injector.buffer == ""
    assert backspaces(injector) == [2000] * 12 + [1000]
    assert controller.typed_text == ""


def test_rollback_does_not_type_and_has_no_typing_delay(fake_engine, injector):
    controller = make_controller(fake_engine, injector, typing_delay_ms=500)
    run_macro(controller, "abc", 13)
    injector.calls.clear()
    controller._sleep = lambda seconds: injector.calls.append(("sleep", seconds))
    run_macro(controller, "{Rollback}", 14)
    assert injector.calls == [("backspace", 3)], "no pause between the Backspaces, no text"


def test_rollback_with_nothing_to_undo_does_nothing(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Rollback}")
    assert injector.calls == []


def test_rollback_is_one_shot(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc", 13)
    injector.buffer = "keep abc"
    run_macro(controller, "{Rollback}", 14)
    run_macro(controller, "{Rollback}", 15)
    run_macro(controller, "{Rollback}{Rollback}", 16)
    assert injector.buffer == "keep "
    assert backspaces(injector) == [3]
    assert controller.undo_last_output() == UndoResult(0, "nothing")


def test_rollback_after_other_keys_deletes_nothing(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc", 13)
    run_macro(controller, "{Ctrl+C}{Rollback}", 14)
    assert backspaces(injector) == []
    assert injector.buffer == "abc"


def test_rollback_after_a_pasted_file_deletes_nothing(fake_engine, injector, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    controller = make_controller(fake_engine, injector)
    run_macro(controller, f"abc{{File {target}}}{{Rollback}}")
    assert backspaces(injector) == []


def test_rollback_inside_a_macro_deletes_what_it_typed_before(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "old", 13)
    run_macro(controller, "new{Rollback}", 14)
    assert injector.buffer == "old", "the macro's own text goes, the earlier output stays"
    assert backspaces(injector) == [3]
    assert controller.undo_last_output() == UndoResult(0, "nothing")


def test_rollback_first_replaces_the_last_output(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc", 13)
    run_macro(controller, "{Rollback}Hello", 14)
    assert injector.buffer == "Hello"
    assert controller.undo_last_output() == UndoResult(5)


def test_a_scroll_before_rollback_does_not_hide_the_last_output(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc", 13)
    run_macro(controller, "{Scroll Down}{Rollback}", 14)
    assert injector.buffer == ""


def test_morse_codes_typing_the_clipboard_and_rolling_it_back(fake_engine, injector, tmp_path):
    injector.clipboard = "Genesis 1:1 In the beginning"
    codes = {"--..--..": "{Clipboard}", "--..-.-.": "{Rollback}"}
    make_controller(fake_engine, injector, codes, tmp_path)
    fake_engine.letters("--..--..")
    assert injector.buffer == "Genesis 1:1 In the beginning"
    fake_engine.letters("--..-.-.")
    assert injector.buffer == ""
    assert backspaces(injector) == [28]
    fake_engine.letters("--..-.-.")
    assert backspaces(injector) == [28], "the second rollback finds nothing"


def test_rollback_stopped_by_esc_leaves_the_rest_undoable(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "x" * 5000, 13)
    original = injector.inject_backspaces

    def esc_after_the_first_piece(count):
        original(count)
        fake_engine.abort_requested = True

    injector.inject_backspaces = esc_after_the_first_piece
    run_macro(controller, "{Rollback}", 14)
    injector.inject_backspaces = original
    assert backspaces(injector) == [2000]
    assert len(injector.buffer) == 3000
    assert controller.undo_last_output() == UndoResult(3000)
    assert injector.buffer == ""


def test_undo_of_a_long_output_goes_in_pieces_too(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "x" * 5000)
    assert controller.undo_last_output() == UndoResult(5000)
    assert backspaces(injector) == [2000, 2000, 1000]


def test_undo_stopped_by_esc_reports_what_was_deleted(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "x" * 5000)
    original = injector.inject_backspaces

    def esc_after_the_first_piece(count):
        original(count)
        fake_engine.abort_requested = True

    injector.inject_backspaces = esc_after_the_first_piece
    assert controller.undo_last_output() == UndoResult(2000, "off")
    injector.inject_backspaces = original
    assert controller.undo_last_output() == UndoResult(3000)
