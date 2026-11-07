"""Undo last output: the length that is tracked, the edge cases, the shortcut plumbing and the settings."""

from __future__ import annotations

import json
import threading

import pytest
from mourse_decoder._native import NATIVE_AVAILABLE
from mourse_decoder.controller import MorseController, UndoResult, typed_length
from mourse_decoder.decoders import Emission
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.function_keys import FunctionKeys
from mourse_decoder.scroller import Scroller
from mourse_decoder.settings import Settings, SettingsStore

SLOT = 13


def make_controller(engine, injector, **settings_changes) -> MorseController:
    controller = MorseController(
        engine, injector, DictionaryManager(), Settings().replace(**settings_changes),
        scroller_factory=lambda **kwargs: Scroller(**kwargs, spawn=lambda body: None),
    )
    controller.start()
    return controller


def run_macro(controller, macro: str) -> None:
    """Type ``macro`` as the function key F13 would."""
    keys = FunctionKeys()
    controller.apply_function_keys(keys.replace_slot(keys.slot(SLOT).replace(macro=macro)))
    assert controller.run_slot(SLOT, block=True) is None


def backspaces(injector) -> list[int]:
    return [count for kind, count in injector.calls if kind == "backspace"]




@pytest.mark.parametrize(
    ("text", "length"),
    [("", 0), ("abc", 3), ("a\nb", 3), ("a\r\nb", 3), ("a\rb", 3), ("\r\n\r\n", 2), ("a\tb", 3), ("😀x", 2), ("é", 1)],
)
def test_typed_length_counts_a_line_break_once(text, length):
    assert typed_length(text) == length


# the basic rule: once, and only after an output


def test_nothing_to_undo_before_any_output(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    assert controller.undo_last_output() == UndoResult(0, "nothing")
    assert injector.calls == []


def test_undo_deletes_the_last_letter_and_only_that(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    fake_engine.letters(".", ".-")  # e, a
    assert injector.buffer == "EA"
    assert controller.undo_last_output() == UndoResult(1)
    assert injector.buffer == "E"
    assert backspaces(injector) == [1]


def test_a_second_undo_has_nothing_to_delete(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    fake_engine.letters(".", ".-")
    controller.undo_last_output()
    for _ in range(3):
        assert controller.undo_last_output() == UndoResult(0, "nothing")
    assert injector.buffer == "E"
    assert backspaces(injector) == [1], "no repeated over-deleting"


def test_a_new_output_arms_the_undo_again(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    fake_engine.letters(".")
    controller.undo_last_output()
    fake_engine.letters(".-")
    assert controller.undo_last_output() == UndoResult(1)
    assert injector.buffer == ""
    assert backspaces(injector) == [1, 1]


def test_a_macro_run_is_one_output(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "Hello World")
    assert controller.undo_last_output() == UndoResult(11)
    assert injector.buffer == ""
    assert backspaces(injector) == [11]


def test_a_word_gap_is_an_output_of_its_own(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    fake_engine.letters(".")
    fake_engine.emit("word_gap")
    assert injector.buffer == "E "
    assert controller.undo_last_output() == UndoResult(1)
    assert injector.buffer == "E"


def test_loops_are_counted_as_typed(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Loop 3 i}ab{EndLoop}")
    assert controller.undo_last_output() == UndoResult(6)


def test_undo_removes_the_deleted_characters_from_the_history(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    fake_engine.letters(".", ".-")
    controller.undo_last_output()
    assert controller.typed_text == "E"


def test_long_text_in_pieces_is_counted_whole(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    text = "x" * (MorseController.TYPE_CHUNK * 2 + 7)
    run_macro(controller, text)
    assert controller.undo_last_output() == UndoResult(len(text))


def test_pasted_text_is_counted_whole(fake_engine, injector):
    class Pasting:
        def __init__(self) -> None:
            self.pasted: list[str] = []

        def try_paste(self, text: str) -> bool:
            self.pasted.append(text)
            return True

    controller = make_controller(fake_engine, injector)
    pasting = Pasting()
    injector.pastes = lambda text: True
    injector.try_paste = pasting.try_paste
    text = "y" * (MorseController.TYPE_CHUNK + 50)
    run_macro(controller, text)
    assert pasting.pasted == [text]
    assert controller.undo_last_output() == UndoResult(len(text))


# special keys and other non-character output


@pytest.mark.parametrize(
    ("macro", "length"),
    [
        ("Hi{Enter}", 3),
        ("Hi{Tab}{Space}", 4),
        ("a\r\nb", 3),
        ("Hi{Ctrl+C}", 0),  # a shortcut may change the text in any way
        ("Hi{Ctrl+C}there", 5),  # only what follows it is known
        ("Hi{Backspace}", 0),
        ("Hi{Backspace 2}x", 1),
        ("{Ctrl+C}", 0),
    ],
)
def test_only_characters_actually_typed_are_counted(fake_engine, injector, macro, length):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, macro)
    result = controller.undo_last_output()
    assert result == (UndoResult(length) if length else UndoResult(0, "nothing"))


def test_german_and_alternative_key_names_count_as_one_character(fake_engine, injector):
    injector.check_shortcut = lambda combo: None  # the stand-in parser knows English names only
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "Hi{Eingabe}{Leertaste}{Return}")
    assert controller.undo_last_output() == UndoResult(5)


def test_a_key_only_macro_makes_the_earlier_output_undeletable(fake_engine, injector):
    """After {Ctrl+A}{Delete} the old length points at text that is gone; deleting it would eat other text."""
    controller = make_controller(fake_engine, injector)
    fake_engine.letters(".", ".-")
    run_macro(controller, "{Ctrl+C}")
    assert controller.undo_last_output() == UndoResult(0, "nothing")
    assert backspaces(injector) == []


def test_a_pasted_file_counts_as_unknown(fake_engine, injector, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    controller = make_controller(fake_engine, injector)
    run_macro(controller, f"abc{{File {target}}}")
    assert controller.undo_last_output() == UndoResult(0, "nothing")


def test_the_clipboard_text_is_counted_but_not_added_to_the_history(fake_engine, injector):
    injector.clipboard = "secret"
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Clipboard}")
    assert controller.typed_text == ""
    assert controller.undo_last_output() == UndoResult(6)
    assert injector.buffer == ""


def test_waiting_changes_nothing(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc")
    run_macro(controller, "{Wait 1}")
    assert controller.undo_last_output() == UndoResult(3)


def test_a_correction_counts_what_replaced_the_erased_text(fake_engine, injector):
    """Wabun dakuten: カ was typed, then one Backspace and ガ. Only ガ is the last output."""
    controller = make_controller(fake_engine, injector)
    controller._emit(Emission("x"))
    controller._emit(Emission("カ"))
    controller._emit(Emission("ガ", erase=1))
    assert injector.buffer == "xガ"
    assert controller.undo_last_output() == UndoResult(1)
    assert injector.buffer == "x"


def test_an_erase_without_text_makes_the_length_unknown(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller._emit(Emission("abc"))
    controller._emit(Emission("", erase=2))
    assert controller.undo_last_output() == UndoResult(0, "nothing")


def test_a_broken_macro_changes_nothing(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc")
    keys = FunctionKeys()
    controller._function_keys = keys.replace_slot(keys.slot(14).replace(macro="x{Loop 3}", enabled=True))
    assert controller.run_slot(14, block=True) is not None
    assert controller.undo_last_output() == UndoResult(3)




def test_a_scroll_macro_leaves_the_last_output_undoable(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc")
    run_macro(controller, "{Scroll Down}")
    run_macro(controller, "{Scroll Stop}")
    assert controller.undo_last_output() == UndoResult(3)


def test_text_typed_next_to_a_scroll_command_is_counted(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "{Scroll Down}xy")
    assert controller.undo_last_output() == UndoResult(2)
    assert injector.buffer == ""


def test_a_scroll_code_of_a_smiley_set_does_not_break_undo(tmp_path, fake_engine, injector):
    (tmp_path / "macros.json").write_text(
        json.dumps({"id": "macros", "codes": {"--..--..": "{Scroll Down}"}}), encoding="utf-8"
    )
    controller = MorseController(
        fake_engine, injector, DictionaryManager(smiley_directory=tmp_path), Settings().replace(smileys="macros"),
        scroller_factory=lambda **kwargs: Scroller(**kwargs, spawn=lambda body: None),
    )
    controller.start()
    fake_engine.letters(".-")
    fake_engine.letters("--..--..")
    assert injector.buffer == "A", "the scroll code typed nothing"
    assert controller.undo_last_output() == UndoResult(1)
    assert injector.buffer == ""


# Esc, failures, busy, off, held modifiers


def test_text_cut_short_by_esc_is_not_counted(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc")
    original = injector.inject_text

    def esc_while_typing(text):
        original(text)
        fake_engine.abort_requested = True

    injector.inject_text = esc_while_typing
    keys = FunctionKeys()
    controller.apply_function_keys(keys.replace_slot(keys.slot(14).replace(macro="defg", trigger="F14")))
    controller.run_slot(14, block=True)
    injector.inject_text = original
    # the cut text is unknown, the earlier 3 characters sit before it: deleting 3 would hit the wrong text
    assert controller.undo_last_output() == UndoResult(0, "nothing")


def test_pieces_typed_before_esc_are_counted(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    text = "x" * (MorseController.TYPE_CHUNK * 3)
    original = injector.inject_text
    typed = []

    def esc_after_second_piece(piece):
        original(piece)
        typed.append(piece)
        if len(typed) == 2:
            fake_engine.abort_requested = True

    injector.inject_text = esc_after_second_piece
    keys = FunctionKeys()
    controller.apply_function_keys(keys.replace_slot(keys.slot(SLOT).replace(macro=text)))
    controller.run_slot(SLOT, block=True)
    # the second piece was cut by Esc and is not counted; the first one is
    assert controller.undo_last_output() == UndoResult(MorseController.TYPE_CHUNK)


def test_a_failing_injector_cannot_lead_to_deleting_twice(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc")

    def broken(count):
        raise RuntimeError("kaputt")

    injector.inject_backspaces = broken
    with pytest.raises(RuntimeError):
        controller.undo_last_output()
    del injector.inject_backspaces
    assert controller.undo_last_output() == UndoResult(0, "nothing")


def test_nothing_happens_while_a_macro_types(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc")
    controller._macro_lock.acquire()
    try:
        assert controller.undo_last_output() == UndoResult(0, "busy")
    finally:
        controller._macro_lock.release()
    assert controller.undo_last_output() == UndoResult(3), "the length was kept"


def test_nothing_happens_while_morse_input_is_off(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc")
    fake_engine.active = False
    assert controller.undo_last_output() == UndoResult(0, "off")
    fake_engine.active = True
    controller.apply_settings(controller.settings.replace(enabled=False))
    assert controller.undo_last_output() == UndoResult(0, "off")
    controller.apply_settings(controller.settings.replace(enabled=True))
    assert controller.undo_last_output() == UndoResult(3)


def test_held_modifiers_delay_the_undo_and_keep_the_length(fake_engine, injector, monkeypatch):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc")
    monkeypatch.setattr(MorseController, "MODIFIER_TIMEOUT_S", 0.1)
    fake_engine.modifiers_released = False
    assert controller.undo_last_output() == UndoResult(0, "modifiers")
    assert backspaces(injector) == [], "Ctrl+Backspace would delete words"
    fake_engine.modifiers_released = True
    assert controller.undo_last_output() == UndoResult(3)


def test_undo_waits_for_the_modifiers_to_be_let_go(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    run_macro(controller, "abc")
    fake_engine.modifiers_released = False
    timer = threading.Timer(0.2, lambda: setattr(fake_engine, "modifiers_released", True))
    timer.start()
    try:
        assert controller.undo_last_output() == UndoResult(3)
    finally:
        timer.cancel()




def test_the_engine_gets_the_undo_shortcut(fake_engine, injector):
    settings = Settings().replace(undo_shortcut="Ctrl+Alt+F8")
    controller = MorseController(fake_engine, injector, DictionaryManager(), settings)
    controller.start()
    assert fake_engine.undo_shortcut == "Ctrl+Alt+F8"
    controller.apply_settings(settings.replace(undo_shortcut=""))
    assert fake_engine.undo_shortcut == ""


def test_poll_undo_counts_presses_since_the_last_call(fake_engine, injector):
    fake_engine.undo_count = 7  # pressed before the app started: not ours
    controller = make_controller(fake_engine, injector)
    assert controller.poll_undo() == 0
    fake_engine.undo_count = 9
    assert controller.poll_undo() == 2
    assert controller.poll_undo() == 0


def test_poll_undo_survives_the_counter_wrapping(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    fake_engine.undo_count = 2**32 - 1
    controller.poll_undo()
    fake_engine.undo_count = 1
    assert controller.poll_undo() == 2


def test_nothing_is_counted_while_stopped(fake_engine, injector):
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings())
    fake_engine.undo_count = 5
    assert controller.poll_undo() == 0


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust-Modul nicht gebaut")
def test_the_real_engine_takes_the_shortcut_and_counts():
    from mourse_decoder import _morse_core as core

    engine = core.MorseEngine()
    before = engine.undo_count
    engine.undo_shortcut = "Ctrl+Alt+F8"
    engine.undo_shortcut = ""
    assert isinstance(before, int) and engine.undo_count == before
    with pytest.raises(ValueError):
        engine.undo_shortcut = "Ctrl+Foo"




def test_the_default_is_no_shortcut():
    assert Settings().undo_shortcut == ""
    assert Settings().validate() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"undo_shortcut": "Ctrl+Foo"},
        {"undo_shortcut": "Ctrl+Shift+Pause"},  # the on/off shortcut
        {"undo_shortcut": "shift+ctrl+PAUSE"},  # the same keys, written differently
        {"undo_shortcut": "Ctrl+Alt+F9", "cycle_shortcut": "ctrl+alt+f9"},
        {"undo_shortcut": "Ctrl+ScrollLock"},  # the Morse key with an extra modifier
        {"undo_shortcut": "F8", "trigger": "F8", "block_shortcut": ""},
    ],
)
def test_invalid_or_clashing_undo_shortcuts_are_refused(changes):
    assert Settings().replace(**changes).validate()


@pytest.mark.parametrize(
    "changes",
    [
        {"undo_shortcut": "Ctrl+Alt+F8"},
        {"undo_shortcut": "Ctrl+Alt+F8", "cycle_shortcut": "Ctrl+Alt+F9"},
        {"undo_shortcut": "Ctrl+Alt+F8", "block_shortcut": ""},
    ],
)
def test_valid_undo_shortcuts_are_accepted(changes):
    assert Settings().replace(**changes).validate() == []


def test_a_clashing_undo_shortcut_in_an_old_file_is_dropped_not_the_whole_file(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"trigger": "F8", "undo_shortcut": "Ctrl+F8", "alphabet": "german"}), encoding="utf-8"
    )
    loaded = SettingsStore(tmp_path).load()
    assert (loaded.trigger, loaded.alphabet, loaded.undo_shortcut) == ("F8", "german", "")


def test_a_function_key_cannot_use_the_undo_shortcut():
    keys = FunctionKeys()
    keys = keys.replace_slot(keys.slot(13).replace(macro="a", trigger="F13"))
    assert keys.validate(Settings().replace(undo_shortcut="Ctrl+Alt+F8")) == []
    errors = keys.validate(Settings().replace(undo_shortcut="F13"))
    assert any("Rückgängig-Kürzel" in e for e in errors)


def test_the_settings_file_round_trips_the_shortcut(tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings(undo_shortcut="Ctrl+Alt+F8"))
    assert json.loads(store.path.read_text(encoding="utf-8"))["undo_shortcut"] == "Ctrl+Alt+F8"
    assert store.load().undo_shortcut == "Ctrl+Alt+F8"


def test_every_language_has_the_new_strings():
    from mourse_decoder.i18n import BUILTIN_LOCALE_DIR

    keys = {
        "tray.undo", "settings.undo_shortcut", "settings.undo_shortcut.hint", "timing.typing", "timing.typing.hint",
        "undo.done", "undo.nothing", "undo.busy", "undo.off", "undo.modifiers", "functionkeys.delay",
        "functionkeys.delay.hint", "functionkeys.macro.delay",
    }
    for code in ("en", "de", "de-saar", "es", "fr", "it", "pt"):
        strings = json.loads((BUILTIN_LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))["strings"]
        assert keys <= set(strings), (code, keys - set(strings))
        assert "{n}" in strings["undo.done"], code
        for reason in ("nothing", "busy", "off", "modifiers"):
            assert strings[f"undo.{reason}"].strip(), (code, reason)
