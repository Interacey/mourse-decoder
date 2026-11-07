"""Typing delay: the global setting, the optional value of a function key, and old files without either."""

from __future__ import annotations

import json
import threading

import pytest
from mourse_decoder._native import NATIVE_AVAILABLE
from mourse_decoder.controller import MorseController, NativeInjector
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.function_keys import FunctionKeys, FunctionKeysStore, Slot
from mourse_decoder.settings import (
    DEFAULT_TYPING_DELAY_MS,
    TYPING_DELAY_RANGE,
    Settings,
    SettingsStore,
    effective_typing_delay,
    typing_delay_error,
)
from mourse_decoder.ui.form import parse_form

# what a 0.1.6 settings.json looks like: every key of that version, none of the new ones
OLD_SETTINGS_FILE = {
    "dot_threshold_ms": 180,
    "letter_gap_ms": 650,
    "word_gap_ms": 1500,
    "trigger": "F8",
    "separate_dash_key": True,
    "dash_trigger": "F9",
    "alphabet": "german",
    "smileys": "standard",
    "enabled": True,
    "language": "de",
    "theme": "dark",
    "sidebar_collapsed": True,
    "notifications": False,
    "block_input": False,
    "block_shortcut": "Ctrl+Shift+Pause",
    "cycle_shortcut": "Ctrl+Alt+F11",
    "cycle_alphabets": ["latin", "german"],
}

# a 0.1.6 function_keys.json: a slot with every field it had, no typing_delay_ms
OLD_FUNCTION_KEYS_FILE = {
    "schema_version": 1,
    "slots": [
        {"slot": 5, "trigger": "Ctrl+F5", "macro": "Hallo{Enter}", "enabled": True, "swallow": True, "label": "Gruß"},
        {"slot": 13, "trigger": "F13", "macro": "x", "enabled": False, "swallow": False, "label": ""},
    ],
}




def test_default_is_the_current_behaviour_of_20_ms():
    assert DEFAULT_TYPING_DELAY_MS == 20
    assert Settings().typing_delay_ms == 20
    assert Settings().validate() == []


@pytest.mark.parametrize("value", [TYPING_DELAY_RANGE[0], 20, TYPING_DELAY_RANGE[1]])
def test_values_in_the_range_are_valid(value):
    assert typing_delay_error(value) is None
    assert Settings(typing_delay_ms=value).validate() == []


@pytest.mark.parametrize("value", [-1, 10001, 100000])
def test_values_outside_the_range_are_refused(value):
    assert "zwischen 0 und 10000" in typing_delay_error(value)
    errors = Settings(typing_delay_ms=value).validate()
    assert any("Tastenanschlag-Verzögerung" in e for e in errors)


@pytest.mark.parametrize("value", ["20", 20.0, True, None, [20]])
def test_other_types_are_refused(value):
    assert "ganze Zahl" in typing_delay_error(value)
    assert Settings(typing_delay_ms=value).validate()


def test_the_form_reads_the_field():
    settings, errors = parse_form({"typing_delay_ms": " 35 "}, Settings())
    assert errors == [] and settings.typing_delay_ms == 35
    settings, errors = parse_form({"typing_delay_ms": "abc"}, Settings())
    assert settings is None and "Tastenanschlag-Verzögerung" in errors[0]
    settings, errors = parse_form({"typing_delay_ms": "-2"}, Settings())
    assert settings is None and "zwischen 0 und 10000" in errors[0]




@pytest.mark.parametrize(
    ("global_ms", "macro_ms", "expected"),
    [(20, None, 20), (60, None, 60), (20, 80, 80), (60, 10, 10), (60, 60, 60)],
)
def test_a_macro_value_wins_over_the_global_one(global_ms, macro_ms, expected):
    assert effective_typing_delay(global_ms, macro_ms) == expected


def make_controller(engine, injector, **settings_changes) -> MorseController:
    controller = MorseController(engine, injector, DictionaryManager(), Settings().replace(**settings_changes))
    controller.start()
    return controller


def keys_with(number: int, **changes) -> FunctionKeys:
    keys = FunctionKeys()
    return keys.replace_slot(keys.slot(number).replace(**changes))


def test_a_morse_letter_types_with_the_global_delay(fake_engine, injector):
    make_controller(fake_engine, injector, typing_delay_ms=45)
    fake_engine.letters(".")
    assert injector.buffer == "E"
    assert injector.delays == [45]


def test_a_slot_without_its_own_value_types_with_the_global_delay(fake_engine, injector):
    controller = make_controller(fake_engine, injector, typing_delay_ms=45)
    controller.apply_function_keys(keys_with(13, macro="abc"))
    assert controller.run_slot(13, block=True) is None
    assert injector.delays == [45]


def test_a_slot_with_its_own_value_overrides_the_global_delay(fake_engine, injector):
    controller = make_controller(fake_engine, injector, typing_delay_ms=45)
    controller.apply_function_keys(keys_with(13, macro="abc", typing_delay_ms=120))
    assert controller.run_slot(13, block=True) is None
    assert injector.delays == [120]


def test_the_override_ends_with_the_macro(fake_engine, injector):
    controller = make_controller(fake_engine, injector, typing_delay_ms=45)
    controller.apply_function_keys(keys_with(13, macro="abc", typing_delay_ms=120))
    controller.run_slot(13, block=True)
    fake_engine.letters(".")
    assert injector.delays == [120, 45], "the next Morse letter is back on the global value"


def test_changing_the_global_value_applies_to_the_next_output(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    fake_engine.letters(".")
    controller.apply_settings(controller.settings.replace(typing_delay_ms=100))
    fake_engine.letters(".")
    controller.apply_function_keys(keys_with(13, macro="x"))
    controller.run_slot(13, block=True)
    assert injector.delays == [20, 100, 100]


def test_an_injector_without_set_typing_delay_still_works(fake_engine, injector):
    class Plain:
        """The injector protocol of 0.1.6: no delay support."""

        def __getattr__(self, name):
            if name == "set_typing_delay":
                raise AttributeError(name)
            return getattr(injector, name)

    controller = MorseController(fake_engine, Plain(), DictionaryManager(), Settings())
    controller.start()
    fake_engine.letters(".")
    assert injector.buffer == "E"


class FakeNative:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def inject_text(self, *args) -> None:
        self.calls.append(args)


def test_the_native_injector_passes_the_delay_on():
    native = FakeNative()
    injector = NativeInjector(native)
    injector.inject_text("a")
    injector.set_typing_delay(70)
    injector.inject_text("b")
    injector.type_text("c")
    assert native.calls == [("a",), ("b", 70), ("c", 70)], "without a delay the native default (20 ms) applies"


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust-Modul nicht gebaut")
def test_the_native_module_uses_the_same_range():
    from mourse_decoder import _morse_core as core

    low, high = TYPING_DELAY_RANGE
    core.inject_text("", low)  # empty text: nothing is typed, only the delay is checked
    core.inject_text("", high)
    core.inject_text("")
    for bad in (high + 1, 10 * high):
        with pytest.raises(ValueError, match=f"zwischen {low} und {high}"):
            core.inject_text("", bad)




def test_an_old_settings_file_loads_unchanged(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(OLD_SETTINGS_FILE, indent=2), encoding="utf-8")
    loaded = SettingsStore(tmp_path).load()
    assert loaded.typing_delay_ms == DEFAULT_TYPING_DELAY_MS
    assert loaded.undo_shortcut == ""
    for key, value in OLD_SETTINGS_FILE.items():
        assert getattr(loaded, key) == (tuple(value) if isinstance(value, list) else value), key
    assert not (tmp_path / "settings.json.bak").exists(), "a valid old file is not treated as broken"
    assert json.loads(path.read_text(encoding="utf-8")) == OLD_SETTINGS_FILE, "loading does not rewrite the file"


def test_settings_with_the_new_value_round_trip(tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings(typing_delay_ms=33))
    assert json.loads(store.path.read_text(encoding="utf-8"))["typing_delay_ms"] == 33
    assert store.load().typing_delay_ms == 33


def test_an_out_of_range_value_in_the_file_resets_the_settings(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({**OLD_SETTINGS_FILE, "typing_delay_ms": 10001}), encoding="utf-8")
    assert SettingsStore(tmp_path).load() == Settings()
    assert (tmp_path / "settings.json.bak").exists()


def test_an_old_function_keys_file_loads_unchanged(tmp_path):
    path = tmp_path / "function_keys.json"
    text = json.dumps(OLD_FUNCTION_KEYS_FILE, indent=2)
    path.write_text(text, encoding="utf-8")
    store = FunctionKeysStore(tmp_path)
    keys, warnings = store.load(None)
    assert warnings == []
    assert not (tmp_path / "function_keys.json.bak").exists()
    assert keys.slot(5) == Slot(5, trigger="Ctrl+F5", macro="Hallo{Enter}", label="Gruß")
    assert keys.slot(5).typing_delay_ms is None and keys.slot(13).typing_delay_ms is None
    assert path.read_text(encoding="utf-8") == text, "loading does not rewrite the file"
    store.save(keys)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == OLD_FUNCTION_KEYS_FILE, "saving slots without a delay writes the same fields as 0.1.6"


def test_a_slot_delay_is_written_and_read_back(tmp_path):
    store = FunctionKeysStore(tmp_path)
    store.save(keys_with(5, trigger="Ctrl+F5", macro="a", typing_delay_ms=75))
    written = json.loads(store.path.read_text(encoding="utf-8"))["slots"]
    assert written == [
        {"slot": 5, "trigger": "Ctrl+F5", "macro": "a", "enabled": True, "swallow": True, "label": "", "typing_delay_ms": 75}
    ]
    loaded, warnings = store.load(None)
    assert warnings == [] and loaded.slot(5).typing_delay_ms == 75


def test_a_slot_with_only_a_delay_is_not_a_default_slot(tmp_path):
    store = FunctionKeysStore(tmp_path)
    store.save(keys_with(20, typing_delay_ms=30))
    assert [s["slot"] for s in json.loads(store.path.read_text(encoding="utf-8"))["slots"]] == [20]


@pytest.mark.parametrize("bad", [-1, 10001, "40", True, 1.5])
def test_a_bad_slot_delay_is_reported(bad):
    errors = keys_with(5, macro="a", typing_delay_ms=bad).validate()
    assert any(e.startswith("F5: ") and "Tastenanschlag-Verzögerung" in e for e in errors)


def test_a_bad_delay_in_the_file_switches_the_slot_off_and_keeps_the_macro(tmp_path):
    entry = {"slot": 5, "trigger": "Ctrl+F5", "macro": "Hallo", "typing_delay_ms": 10001}
    (tmp_path / "function_keys.json").write_text(json.dumps({"schema_version": 1, "slots": [entry]}), encoding="utf-8")
    store = FunctionKeysStore(tmp_path)
    keys, warnings = store.load(None)
    assert len(warnings) == 1 and "F5" in warnings[0] and "zwischen 0 und 10000" in warnings[0]
    slot = keys.slot(5)
    assert (slot.macro, slot.enabled, slot.typing_delay_ms) == ("Hallo", False, None)
    store.save(keys)  # the repaired table can be written again


def test_unknown_fields_of_a_slot_are_still_kept(tmp_path):
    entry = {"slot": 5, "trigger": "Ctrl+F5", "macro": "a", "typing_delay_ms": 50, "future": 1}
    (tmp_path / "function_keys.json").write_text(json.dumps({"schema_version": 1, "slots": [entry]}), encoding="utf-8")
    store = FunctionKeysStore(tmp_path)
    keys, _ = store.load(None)
    store.save(keys)
    saved = json.loads(store.path.read_text(encoding="utf-8"))["slots"][0]
    assert saved["future"] == 1 and saved["typing_delay_ms"] == 50


def test_two_threads_with_different_delays_do_not_mix(fake_engine, injector):
    """One macro runs at a time, so a slot's delay cannot leak into a Morse letter typed meanwhile."""
    controller = make_controller(fake_engine, injector, typing_delay_ms=45)
    controller.apply_function_keys(keys_with(13, macro="abc", typing_delay_ms=120))
    release = threading.Event()
    original = injector.inject_text

    def slow(text):
        release.wait(2)
        original(text)

    injector.inject_text = slow
    assert controller.run_slot(13) is None
    fake_engine.letters(".")  # dropped: the macro still types
    release.set()
    controller._macro_lock.acquire()
    controller._macro_lock.release()
    assert injector.delays == [120]
    assert injector.buffer == "abc"
