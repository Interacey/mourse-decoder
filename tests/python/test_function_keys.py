"""Function keys: slot model, default triggers, the JSON file and the controller side (with fakes)."""

from __future__ import annotations

import json
import threading
import time

import mourse_decoder.controller as controller_module
import pytest
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.function_keys import (
    MAX_MACRO_LENGTH,
    SCHEMA_VERSION,
    SLOT_COUNT,
    FunctionKeys,
    FunctionKeysStore,
    Slot,
    default_slot,
    default_trigger,
    describe_trigger,
    slot_name,
)
from mourse_decoder.settings import Settings, is_valid_binding, normalize_binding, same_binding


def check(combo: str) -> None:
    """Stand-in for the Rust combo parser: modifiers plus one known key."""
    *modifiers, key = (part.strip().lower() for part in combo.split("+"))
    if any(m not in {"ctrl", "alt", "shift", "win"} for m in modifiers) or key not in {"c", "v", "enter", "tab"}:
        raise ValueError(combo)


def with_slot(keys: FunctionKeys | None = None, number: int = 5, **changes) -> FunctionKeys:
    keys = keys or FunctionKeys()
    return keys.replace_slot(keys.slot(number).replace(**changes))


# default triggers


def test_slot_names():
    assert slot_name(1) == "F1"
    assert Slot(57).name == "F57"


def test_f1_to_f12_have_no_default_trigger_and_f13_to_f24_are_the_bare_keys():
    assert [default_trigger(n) for n in range(1, 13)] == [""] * 12
    assert [default_trigger(n) for n in range(13, 25)] == [f"F{n}" for n in range(13, 25)]


def test_virtual_slots_get_76_distinct_valid_triggers():
    triggers = [default_trigger(n) for n in range(25, SLOT_COUNT + 1)]
    assert len(triggers) == 76
    assert len(set(triggers)) == 76
    assert all(is_valid_binding(t) for t in triggers)
    assert all(normalize_binding(t) == t for t in triggers), "already in the form the hook prints"
    # no two of them are the same binding in another spelling
    assert len({normalize_binding(t).lower() for t in triggers}) == 76


def test_virtual_triggers_run_from_four_modifiers_down_to_two():
    triggers = [default_trigger(n) for n in range(25, SLOT_COUNT + 1)]
    sizes = [t.count("+") for t in triggers]
    assert sizes == sorted(sizes, reverse=True)
    assert sizes.count(4) == 12 and sizes.count(3) == 48 and sizes.count(2) == 16
    assert triggers[0] == "Ctrl+Alt+Shift+Win+F13"
    assert triggers[11] == "Ctrl+Alt+Shift+Win+F24"
    assert triggers[12] == "Ctrl+Alt+Shift+F13"
    assert triggers[-1] == "Ctrl+Shift+F16"


def test_default_triggers_do_not_clash_with_each_other_or_the_default_settings():
    all_defaults = [default_trigger(n) for n in range(13, SLOT_COUNT + 1)]
    for i, first in enumerate(all_defaults):
        assert not any(same_binding(first, second) for second in all_defaults[i + 1 :])
    keys = FunctionKeys()
    keys = FunctionKeys(tuple(s.replace(macro="x") for s in keys.slots))
    assert keys.validate(Settings(), check) == []


def test_default_trigger_rejects_numbers_out_of_range():
    for bad in (0, 101, -1):
        with pytest.raises(ValueError):
            default_trigger(bad)


def test_describe_trigger_uses_the_display_names():
    assert describe_trigger("") == ""
    assert describe_trigger("Ctrl+Alt+F13") == "Ctrl + Alt + F13"
    assert describe_trigger("VK124") == "F13"


def test_normalize_binding_spellings():
    assert normalize_binding(" strg+Shift+vk124 ") == "Ctrl+Shift+F13"
    assert normalize_binding("win+alt+f24") == "Alt+Win+F24"
    assert normalize_binding("Ctrl+F25") == "Ctrl+F25", "invalid input stays as it is"
    assert same_binding("Control+VK124", "ctrl+F13")
    assert not same_binding("Ctrl+F13", "F13")


# data model


def test_new_function_keys_have_100_default_slots():
    keys = FunctionKeys()
    assert [s.slot for s in keys.slots] == list(range(1, 101))
    assert all(s.is_default() for s in keys.slots)
    assert keys.active_slots() == []
    assert keys.validate() == []


def test_slot_is_active_only_with_trigger_macro_and_enabled():
    base = Slot(5, trigger="Ctrl+F5", macro="hi")
    assert base.active
    assert not base.replace(enabled=False).active
    assert not base.replace(macro="  ").active
    assert not base.replace(trigger="").active


def test_slot_lookup_and_replace_check_the_range():
    keys = FunctionKeys()
    with pytest.raises(ValueError):
        keys.slot(0)
    with pytest.raises(ValueError):
        keys.replace_slot(Slot(101))
    assert keys.reset_slot(5).slot(5).is_default()


# validate


def test_validate_reports_duplicate_triggers_of_active_slots_only():
    keys = with_slot(None, 1, trigger="Ctrl+F5", macro="a")
    keys = with_slot(keys, 2, trigger="Strg+F5", macro="b")
    errors = keys.validate()
    assert errors == ["F1 und F2 haben denselben Auslöser (Strg+F5)."]
    assert with_slot(keys, 2, enabled=False).validate() == []
    assert with_slot(keys, 2, macro="").validate() == []


def test_validate_reports_invalid_triggers_even_without_a_macro():
    errors = with_slot(None, 3, trigger="Hyper+F5").validate()
    assert errors == ["F3: Ungültiger Auslöser 'Hyper+F5'."]
    assert with_slot(None, 3, trigger="F25").validate() == ["F3: Ungültiger Auslöser 'F25'."]


def test_validate_reports_macro_errors_with_the_slot_name():
    errors = with_slot(None, 7, trigger="Ctrl+F5", macro="{Schleife 2}a").validate(None, check)
    assert len(errors) == 1 and errors[0].startswith("F7: ")
    assert with_slot(None, 7, trigger="Ctrl+F5", macro="{Ctrl+C}").validate(None, check) == []
    assert with_slot(None, 7, macro="{^_^} {Ctrl+C}").validate(None, check) == []
    # a disabled slot may keep a broken macro
    assert with_slot(None, 7, macro="{Schleife 2}a", enabled=False).validate(None, check) == []


def test_validate_limits_macro_length_and_label():
    too_long = "x" * (MAX_MACRO_LENGTH + 1)
    assert with_slot(None, 1, macro=too_long).validate() == [f"F1: Das Makro ist länger als {MAX_MACRO_LENGTH} Zeichen."]
    assert with_slot(None, 1, macro="x" * MAX_MACRO_LENGTH).validate() == []
    assert with_slot(None, 1, label="y" * 101).validate()


def test_validate_rejects_a_wrong_number_of_slots():
    assert FunctionKeys(FunctionKeys().slots[:99]).validate()


def test_validate_finds_clashes_with_the_morse_keys_and_shortcuts():
    keys = with_slot(None, 13, macro="x")  # F13, bare
    assert keys.validate(Settings(trigger="F13")) == ["F13 (F13) kollidiert mit der Morse-Taste (F13)."]
    # the Morse key also reacts with extra modifiers, so Ctrl+F13 clashes too ...
    assert with_slot(None, 1, trigger="Ctrl+F13", macro="x").validate(Settings(trigger="F13"))
    # ... but a Morse key that needs more modifiers is not triggered by the slot
    assert keys.validate(Settings(trigger="Ctrl+F13")) == []
    # dash key only counts while it is in use
    assert keys.validate(Settings(dash_trigger="F13")) == []
    assert keys.validate(Settings(dash_trigger="F13", separate_dash_key=True))
    assert keys.validate(Settings(block_shortcut="F13"))
    assert keys.validate(Settings(cycle_shortcut="VK124")), "VK124 is F13"
    assert with_slot(None, 13).validate(Settings(trigger="F13")) == [], "empty slots are ignored"


# file


def store(tmp_path) -> FunctionKeysStore:
    return FunctionKeysStore(tmp_path)


def test_missing_file_gives_defaults(tmp_path):
    keys, warnings = store(tmp_path).load(check)
    assert keys == FunctionKeys() and warnings == []
    assert not (tmp_path / "function_keys.json").exists()


def test_roundtrip_writes_only_changed_slots(tmp_path):
    s = store(tmp_path)
    keys = with_slot(None, 5, trigger="Ctrl+F5", macro="Hallo [i]\n", swallow=False, label="Gruß")
    keys = with_slot(keys, 30, enabled=False)
    s.save(keys)
    data = json.loads((tmp_path / "function_keys.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    assert [e["slot"] for e in data["slots"]] == [5, 30]
    assert data["slots"][0] == {
        "slot": 5, "trigger": "Ctrl+F5", "macro": "Hallo [i]\n", "enabled": True, "swallow": False, "label": "Gruß",
    }
    loaded, warnings = s.load(check)
    assert warnings == []
    assert loaded == keys
    assert not list(tmp_path.glob("*.tmp"))


def test_save_is_readable_text_with_indent_and_unicode(tmp_path):
    store(tmp_path).save(with_slot(None, 1, macro="ä"))
    text = (tmp_path / "function_keys.json").read_text(encoding="utf-8")
    assert "ä" in text and "\n  " in text


def test_default_slots_are_not_written(tmp_path):
    store(tmp_path).save(FunctionKeys())
    assert json.loads((tmp_path / "function_keys.json").read_text(encoding="utf-8"))["slots"] == []


def test_save_refuses_invalid_slots_and_keeps_the_old_file(tmp_path):
    s = store(tmp_path)
    s.save(with_slot(None, 1, macro="a"))
    before = (tmp_path / "function_keys.json").read_bytes()
    with pytest.raises(ValueError, match="Ungültiger Auslöser"):
        s.save(with_slot(None, 1, trigger="nonsense"))
    assert (tmp_path / "function_keys.json").read_bytes() == before


def test_broken_file_is_moved_to_bak_and_defaults_are_used(tmp_path):
    path = tmp_path / "function_keys.json"
    path.write_text("{ das ist kein JSON", encoding="utf-8")
    keys, warnings = store(tmp_path).load(check)
    assert keys == FunctionKeys()
    assert len(warnings) == 1 and "function_keys.json.bak" in warnings[0]
    assert (tmp_path / "function_keys.json.bak").read_text(encoding="utf-8") == "{ das ist kein JSON"
    assert not path.exists(), "the next save writes a fresh file"


@pytest.mark.parametrize(
    "content",
    ["[]", '{"slots": {}}', '{"schema_version": "1"}', '{"schema_version": 0}', '{"slots": [], "slots": []}'],
)
def test_wrong_structure_counts_as_broken(tmp_path, content):
    (tmp_path / "function_keys.json").write_text(content, encoding="utf-8")
    keys, warnings = store(tmp_path).load(check)
    assert keys == FunctionKeys() and warnings
    assert (tmp_path / "function_keys.json.bak").is_file()


def test_unknown_fields_survive_a_load_and_save(tmp_path):
    (tmp_path / "function_keys.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "future": {"a": 1},
                "slots": [{"slot": 4, "macro": "x", "color": "red", "trigger": "Alt+F4"}],
            }
        ),
        encoding="utf-8",
    )
    s = store(tmp_path)
    keys, warnings = s.load(check)
    assert warnings == []
    assert keys.slot(4).extra == {"color": "red"}
    s.save(with_slot(keys, 4, macro="y"))
    data = json.loads((tmp_path / "function_keys.json").read_text(encoding="utf-8"))
    assert data["future"] == {"a": 1}
    assert data["slots"][0]["color"] == "red" and data["slots"][0]["macro"] == "y"


def test_a_newer_schema_is_read_but_never_overwritten(tmp_path):
    path = tmp_path / "function_keys.json"
    original = json.dumps({"schema_version": SCHEMA_VERSION + 1, "slots": [{"slot": 2, "macro": "neu"}]})
    path.write_text(original, encoding="utf-8")
    s = store(tmp_path)
    keys, warnings = s.load(check)
    assert keys.slot(2).macro == "neu"
    assert len(warnings) == 1 and "neueren Version" in warnings[0]
    with pytest.raises(ValueError, match="neueren Version"):
        s.save(keys)
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "function_keys.json.bak").exists()


def test_trigger_spelling_is_normalized_on_load(tmp_path):
    (tmp_path / "function_keys.json").write_text(
        json.dumps({"schema_version": 1, "slots": [{"slot": 3, "trigger": "strg+vk124", "macro": "x"}]}),
        encoding="utf-8",
    )
    keys, warnings = store(tmp_path).load(check)
    assert keys.slot(3).trigger == "Ctrl+F13" and warnings == []


def test_a_macro_that_does_not_parse_is_kept_but_switched_off(tmp_path):
    path = tmp_path / "function_keys.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "slots": [
                    {"slot": 6, "trigger": "Ctrl+F6", "macro": "{Schleife 2}kaputt"},
                    {"slot": 7, "trigger": "Ctrl+F7", "macro": "gut"},
                ],
            }
        ),
        encoding="utf-8",
    )
    keys, warnings = store(tmp_path).load(check)
    assert keys.slot(6).macro == "{Schleife 2}kaputt" and not keys.slot(6).enabled
    assert keys.slot(7).enabled and keys.slot(7).active
    assert len(warnings) == 1 and warnings[0].startswith("F6: ausgeschaltet.")
    assert (tmp_path / "function_keys.json.bak").is_file(), "the file is saved before the next write changes it"
    assert path.is_file()


def test_bad_entries_are_reported_and_skipped(tmp_path):
    entries = [
        "kein objekt",
        {"slot": 0},
        {"slot": 101},
        {"slot": True},
        {"slot": 8, "macro": 5},
        {"slot": 9, "enabled": "ja"},
        {"slot": 10, "macro": "erster"},
        {"slot": 10, "macro": "zweiter"},
        {"slot": 11, "macro": "ok"},
    ]
    (tmp_path / "function_keys.json").write_text(json.dumps({"schema_version": 1, "slots": entries}), encoding="utf-8")
    keys, warnings = store(tmp_path).load(check)
    assert len(warnings) == 7
    assert keys.slot(10).macro == "erster" and keys.slot(11).macro == "ok"
    assert keys.slot(8).is_default() and keys.slot(9).is_default()


def test_a_too_long_macro_in_the_file_is_switched_off(tmp_path):
    (tmp_path / "function_keys.json").write_text(
        json.dumps({"schema_version": 1, "slots": [{"slot": 2, "trigger": "F5", "macro": "x" * (MAX_MACRO_LENGTH + 1)}]}),
        encoding="utf-8",
    )
    keys, warnings = store(tmp_path).load(check)
    assert not keys.slot(2).enabled and len(warnings) == 1


def test_default_slot_helper_matches_the_default_model():
    assert default_slot(25) == FunctionKeys().slot(25)


# controller


def make_controller(engine, injector, listener=None, **settings_changes) -> MorseController:
    controller = MorseController(
        engine, injector, DictionaryManager(), Settings().replace(**settings_changes), listener=listener
    )
    controller.start()
    return controller


def macro_keys(**macros_by_slot: str) -> FunctionKeys:
    keys = FunctionKeys()
    for name, macro in macros_by_slot.items():
        n = int(name[1:])
        keys = keys.replace_slot(keys.slot(n).replace(trigger=keys.slot(n).trigger or f"Ctrl+F{n}", macro=macro))
    return keys


def test_apply_installs_only_active_slots(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    keys = macro_keys(F13="a", F40="b")
    keys = with_slot(keys, 41, macro="c", enabled=False)
    keys = with_slot(keys, 42, macro="")
    keys = with_slot(keys, 43, macro="d", swallow=False)
    controller.apply_function_keys(keys)
    assert fake_engine.function_keys == [
        (13, "F13", True),
        (40, default_trigger(40), True),
        (43, default_trigger(43), False),
    ]
    assert controller.function_keys is keys


def test_apply_rejects_invalid_slots_and_changes_nothing(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    good = macro_keys(F13="a")
    controller.apply_function_keys(good)
    with pytest.raises(ValueError, match="denselben Auslöser"):
        controller.apply_function_keys(with_slot(with_slot(None, 1, trigger="F5", macro="a"), 2, trigger="F5", macro="b"))
    with pytest.raises(ValueError, match="kollidiert"):
        controller.apply_function_keys(with_slot(None, 1, trigger="Ctrl+Shift+Pause", macro="x"))
    assert controller.function_keys is good
    assert fake_engine.function_keys == [(13, "F13", True)]


def test_apply_checks_macros_with_the_injector(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    with pytest.raises(ValueError, match="F2: "):
        controller.apply_function_keys(with_slot(None, 2, trigger="F5", macro="{Alt+Foo}{Schleife 1}x"))


def test_a_refusal_by_the_hook_keeps_the_old_slots(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    good = macro_keys(F13="a")
    controller.apply_function_keys(good)
    fake_engine.set_function_keys_error = "F14 kollidiert mit der Morse-Taste"
    with pytest.raises(ValueError, match="Morse-Taste"):
        controller.apply_function_keys(macro_keys(F14="b"))
    assert controller.function_keys is good


def test_start_installs_the_slots_and_stop_removes_them(fake_engine, injector):
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings())
    controller.apply_function_keys(macro_keys(F13="a"))
    assert fake_engine.function_keys == [], "not running yet, the hook is not there"
    controller.start()
    assert fake_engine.function_keys == [(13, "F13", True)]
    controller.stop()
    assert fake_engine.function_keys == []


def test_changing_the_morse_key_onto_a_slot_trigger_is_refused(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="a"))
    with pytest.raises(ValueError, match="kollidiert"):
        controller.apply_settings(controller.settings.replace(trigger="F13"))
    assert controller.settings.trigger == "ScrollLock"
    controller.apply_settings(controller.settings.replace(trigger="Ctrl+F13"))  # needs more modifiers: fine


def test_run_slot_types_the_macro(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="Hallo{Ctrl+C}"))
    assert controller.run_slot(13, block=True) is None
    assert injector.calls == [("text", "Hallo"), ("keys", "Ctrl+C")]
    assert fake_engine.reset_calls == 1, "clicks made meanwhile are dropped like after any macro"


def test_macros_can_use_the_loop_counter(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="{Schleife 3}[i]{SchleifenEnde}"))
    controller.run_slot(13, block=True)
    assert injector.buffer == "012"


def test_run_slot_gives_the_reason_when_it_does_not_run(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(with_slot(macro_keys(F13="a"), 14, macro="b", enabled=False))
    assert "kein Makro" in controller.run_slot(20, block=True)
    assert "ausgeschaltet" in controller.run_slot(14, block=True)
    fake_engine.active = False
    assert "Morse-Eingabe ist ausgeschaltet" in controller.run_slot(13, block=True)
    fake_engine.active = True
    controller.apply_settings(controller.settings.replace(enabled=False))
    assert "Morse-Eingabe ist ausgeschaltet" in controller.run_slot(13, block=True)
    assert injector.calls == []


def test_run_slot_reports_a_broken_macro_before_anything_is_typed(fake_engine, injector, listener):
    controller = make_controller(fake_engine, injector, listener=listener)
    controller.apply_function_keys(macro_keys(F13="a{Ctrl+C}"))
    controller._function_keys = macro_keys(F13="a{Loop 2}")  # as if the file was edited behind the back
    assert "EndLoop" in controller.run_slot(13, block=True)
    controller._function_keys = macro_keys(F13="a{Datei C:/gibt/es/nicht/x.txt}")
    assert controller.run_slot(13, block=True) is None, "a missing file only shows when the macro runs"
    assert injector.calls == [] and listener.errors and listener.errors[0].startswith("F13: ")


def wait_until(condition, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_polling_starts_fired_slots_on_their_own_threads(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="eins", F14="zwei"))
    fake_engine.fired_slots = [13]
    assert controller.poll_function_keys() == [13]
    assert wait_until(lambda: injector.buffer == "eins")
    assert wait_until(lambda: not controller._macro_lock.locked())
    fake_engine.fired_slots = [14, 99]  # 99 has no macro
    assert controller.poll_function_keys() == [14]
    assert wait_until(lambda: injector.buffer == "einszwei")


def test_polling_does_nothing_while_stopped(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="a"))
    controller.stop()
    fake_engine.fired_slots = [13]
    assert controller.poll_function_keys() == []
    assert injector.calls == []


def test_a_second_trigger_during_a_running_macro_is_ignored(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="a", F14="b"))
    release = threading.Event()
    inside = threading.Event()
    original = injector.inject_text

    def slow(text):
        inside.set()
        release.wait(3)
        original(text)

    injector.inject_text = slow
    assert controller.run_slot(13) is None
    assert inside.wait(3)
    assert controller.run_slot(14) == "Ein anderes Makro läuft noch."
    fake_engine.fired_slots = [14]
    assert controller.poll_function_keys() == []
    fake_engine.letters(".-")  # a Morse letter meanwhile is dropped, not typed in between
    release.set()
    assert wait_until(lambda: not controller._macro_lock.locked())
    assert injector.buffer == "a"
    # once it is done the next one runs
    injector.inject_text = original
    assert controller.run_slot(14, block=True) is None
    assert injector.buffer == "ab"


def test_a_morse_macro_in_progress_blocks_a_slot(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="a"))
    assert controller._macro_lock.acquire(blocking=False)
    try:
        assert controller.run_slot(13, block=True) == "Ein anderes Makro läuft noch."
    finally:
        controller._macro_lock.release()
    assert controller.run_slot(13, block=True) is None


def test_a_slot_waits_until_the_modifiers_are_let_go(fake_engine, injector, monkeypatch):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="a"))
    monkeypatch.setattr(controller, "POLL_S", 0.01)
    fake_engine.modifiers_released = False
    threading.Timer(0.1, lambda: setattr(fake_engine, "modifiers_released", True)).start()
    start = time.monotonic()
    assert controller.run_slot(13, block=True) is None
    assert time.monotonic() - start >= 0.09
    assert injector.buffer == "a"


def test_a_slot_gives_up_when_the_modifiers_stay_down(fake_engine, injector, listener, monkeypatch):
    controller = make_controller(fake_engine, injector, listener=listener)
    controller.apply_function_keys(macro_keys(F13="a"))
    monkeypatch.setattr(controller, "POLL_S", 0.01)
    monkeypatch.setattr(controller, "MODIFIER_TIMEOUT_S", 0.05)
    fake_engine.modifiers_released = False
    controller.run_slot(13, block=True)
    assert injector.calls == []
    assert len(listener.errors) == 1 and listener.errors[0].startswith("F13: ") and "gedrückt" in listener.errors[0]
    assert not controller._macro_lock.locked()


def test_double_esc_aborts_a_running_slot_macro(fake_engine, injector, listener, monkeypatch):
    controller = make_controller(fake_engine, injector, listener=listener)
    controller.apply_function_keys(macro_keys(F13="vorher{Warte 99999}danach"))
    monkeypatch.setattr(controller_module.time, "sleep", lambda s: setattr(fake_engine, "abort_requested", True))
    controller.run_slot(13, block=True)
    assert injector.calls == [("text", "vorher")]
    assert listener.errors == [], "the user did it on purpose"
    assert not controller._macro_lock.locked()


def test_double_esc_also_aborts_morse_output(fake_engine, injector, listener, monkeypatch):
    controller = make_controller(fake_engine, injector, listener=listener)
    controller._decoder = type("D", (), {"decode": lambda s, q: controller_module.Emission("a{Warte 99999}b"), "word_gap": lambda s: None})()
    monkeypatch.setattr(controller_module.time, "sleep", lambda s: setattr(fake_engine, "abort_requested", True))
    fake_engine.letters(".-")
    assert injector.buffer == "a" and listener.errors == []


def test_an_old_double_esc_does_not_abort_the_next_macro(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(macro_keys(F13="a{Ctrl+C}b"))
    fake_engine.abort_requested = True  # pressed while nothing ran
    controller.run_slot(13, block=True)
    assert injector.buffer == "ab"
    fake_engine.abort_requested = True
    fake_engine.letters(".-")
    assert injector.buffer == "abA"


def test_errors_in_a_slot_macro_reach_the_listener_with_the_slot_name(fake_engine, injector, listener):
    controller = make_controller(fake_engine, injector, listener=listener)
    controller.apply_function_keys(macro_keys(F13="{Zwischenablage}"))
    injector.clipboard = None
    controller.run_slot(13, block=True)
    assert len(listener.errors) == 1 and listener.errors[0].startswith("F13: ")
    assert not controller._macro_lock.locked()


# triggers that would break normal use of the keyboard


@pytest.mark.parametrize(
    "trigger",
    ["VK27", "Ctrl+VK27", "ControlRight", "AltGr", "VK16", "strg+vk17", "VK91", "VK20", "VK65", "Shift+VK65", "VK13",
     "VK8", "Space", "Shift+Space", "VK48", "VK100", "MouseRight"],
)
def test_validate_refuses_esc_modifiers_and_typing_keys_as_trigger(trigger):
    errors = with_slot(trigger=trigger, macro="x").validate()
    assert any("nicht erlaubt" in e and e.startswith("F5: ") for e in errors), (trigger, errors)


@pytest.mark.parametrize(
    "trigger",
    ["Ctrl+VK65", "Alt+Space", "Win+VK13", "F13", "F5", "Shift+F13", "Insert", "Pause", "MouseLeft", "Mouse5",
     "Shift+MouseRight", "Ctrl+MouseMiddle", "VK112"],
)
def test_validate_accepts_shortcuts_and_rare_keys_as_trigger(trigger):
    assert with_slot(trigger=trigger, macro="x").validate() == []


def test_a_right_click_slot_is_fine_when_the_click_is_not_swallowed():
    assert with_slot(trigger="MouseRight", macro="x", swallow=False).validate() == []


def test_load_switches_off_a_slot_on_esc_and_keeps_the_rest(tmp_path):
    store = FunctionKeysStore(tmp_path)
    slots = [{"slot": 5, "trigger": "VK27", "macro": "a"}, {"slot": 6, "trigger": "Ctrl+F6", "macro": "b"}]
    store.path.write_text(json.dumps({"schema_version": 1, "slots": slots}), encoding="utf-8")
    keys, warnings = store.load()
    assert keys.slot(5).enabled is False and keys.slot(5).trigger == "VK27"
    assert keys.slot(6).active
    assert len(warnings) == 1 and warnings[0].startswith("F5: ausgeschaltet") and "Esc" in warnings[0]


def test_a_slot_without_trigger_can_still_be_run_from_the_ui(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(with_slot(trigger="", macro="Hallo"))
    assert controller.run_slot(5, block=True) is None
    assert injector.calls == [("text", "Hallo")]
