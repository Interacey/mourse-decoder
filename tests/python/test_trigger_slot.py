"""MorseController.trigger_slot: the entry for callers outside the program, with fakes (no typing, no Rust)."""

from __future__ import annotations

import threading
import time

import mourse_decoder.controller as controller_module
import pytest
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.function_keys import MAX_MACRO_LENGTH, FunctionKeys, Slot
from mourse_decoder.settings import Settings


def make_controller(engine, injector, listener=None) -> MorseController:
    controller = MorseController(engine, injector, DictionaryManager(), Settings(), listener=listener)
    controller.start()
    return controller


def keys_with(number: int = 13, macro: str = "hi", **changes) -> FunctionKeys:
    """Hand-built slots: they bypass the loader and ``apply_function_keys``, which refuse some of them."""
    keys = FunctionKeys()
    return keys.replace_slot(keys.slot(number).replace(**{"macro": macro, "triggerable": True, **changes}))


def armed(engine, injector, macro: str = "hi", listener=None, **changes) -> MorseController:
    controller = make_controller(engine, injector, listener)
    controller._function_keys = keys_with(13, macro, **changes)
    return controller


def wait_until(condition, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def idle(controller) -> bool:
    return wait_until(lambda: not controller._macro_lock.locked())


# started

def test_ok_types_the_macro_on_its_own_thread_like_a_function_key(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller.apply_function_keys(keys_with(13, "Hallo{Ctrl+C}"))  # valid for the real checks too
    threads: list[str] = []
    original = injector.inject_text

    def note_thread(text):
        threads.append(threading.current_thread().name)
        original(text)

    injector.inject_text = note_thread
    assert controller.trigger_slot(13) == "ok"
    assert idle(controller)
    assert injector.calls == [("text", "Hallo"), ("keys", "Ctrl+C")]
    assert threads == ["macro-F13"], "not the caller's thread"
    assert fake_engine.reset_calls == 1, "clicks made meanwhile are dropped, like after run_slot"


def test_ok_uses_the_slots_typing_delay_like_run_slot(fake_engine, injector):
    controller = armed(fake_engine, injector, "abc", typing_delay_ms=77)
    assert controller.trigger_slot(13) == "ok" and idle(controller)
    assert injector.typed_with == [("abc", 77)]


def test_a_slot_without_a_trigger_key_can_be_triggered(fake_engine, injector):
    controller = armed(fake_engine, injector, "x", trigger="")
    assert controller.trigger_slot(13) == "ok" and idle(controller)
    assert injector.buffer == "x"


def test_esc_aborts_a_triggered_macro_like_any_slot_macro(fake_engine, injector, listener):
    controller = armed(fake_engine, injector, "ab{Enter}", listener=listener)
    real_inject = injector.inject_text
    injector.inject_text = lambda text: (real_inject(text), setattr(fake_engine, "abort_requested", True))
    assert controller.trigger_slot(13) == "ok" and idle(controller)
    assert injector.calls == [("text", "ab")]
    assert listener.errors == [], "the user did it on purpose"


def test_errors_while_it_runs_reach_the_listener_with_the_slot_name(fake_engine, injector, listener):
    controller = armed(fake_engine, injector, "{Ctrl+C}", listener=listener)
    injector.press_shortcut = lambda combo: (_ for _ in ()).throw(RuntimeError("Tasten kaputt"))
    assert controller.trigger_slot(13) == "ok" and idle(controller)
    assert listener.errors == ["F13: Tasten kaputt"]


# unknown_macro

@pytest.mark.parametrize("number", [0, 101, -1, 10**100, True, False, None, "F5", "13", 13.0, 5.5, [13], (13,), b"13", object()])
def test_a_number_that_is_no_slot_is_unknown_and_never_raises(fake_engine, injector, listener, number):
    controller = armed(fake_engine, injector, listener=listener)
    assert controller.trigger_slot(number) == "unknown_macro"
    assert injector.calls == [] and listener.errors == [] and not controller._macro_lock.locked()


def test_a_slot_that_is_not_in_the_table_is_unknown(fake_engine, injector):
    controller = armed(fake_engine, injector)
    controller._function_keys = FunctionKeys(controller._function_keys.slots[:50])
    assert controller.trigger_slot(80) == "unknown_macro"
    assert injector.calls == []


# not_allowed

@pytest.mark.parametrize(
    "changes",
    [
        {"triggerable": False},
        {"triggerable": "yes"},
        {"triggerable": 1},
        {"triggerable": None},
        {"enabled": False},
        {"enabled": "yes"},
        {"macro": ""},
        {"macro": "  \n"},
        {"macro": 5},
        {"macro": None},
    ],
)
def test_not_allowed_when_the_slot_is_not_triggerable_enabled_or_filled(fake_engine, injector, listener, changes):
    controller = armed(fake_engine, injector, listener=listener)
    controller._function_keys = keys_with(13, **{"macro": "hi", **changes})
    assert controller.trigger_slot(13) == "not_allowed"
    assert injector.calls == [] and listener.errors == [] and not controller._macro_lock.locked()


def test_a_default_slot_is_not_allowed(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    assert controller.trigger_slot(13) == "not_allowed"
    assert controller.trigger_slot(100) == "not_allowed"


@pytest.mark.parametrize(
    "macro",
    [
        "{Clipboard}",
        "{Zwischenablage}",
        "{ clipboard }",
        "before{Clipboard}after",
        "{Scrape Name: | \\n | => Hi $1}",
        "{Ausschnitt A | B | => $1}",
        "{File C:/Windows/notepad.exe}",
        "{Datei C:/x.txt|C:/y.txt}",
        "{Folder C:/Temp}",
        "{Loop 2}{Clipboard}{EndLoop}",
        "{Loop 1}{Loop 1}{File C:/a}{EndLoop}{EndLoop}",
        "{Ctrl+C}{Clipboard}",
        "{Wait 10}{Datei C:/a}",
    ],
)
def test_a_sensitive_macro_is_refused_even_when_the_flag_is_set_in_the_data(fake_engine, injector, listener, macro):
    controller = armed(fake_engine, injector, macro, listener=listener)
    assert controller._function_keys.slot(13).triggerable is True, "the data says yes (bypassing the loader)"
    injector.clipboard = "geheim"
    assert controller.trigger_slot(13) == "not_allowed"
    assert injector.calls == [] and injector.buffer == "", "the injector got nothing"
    assert listener.errors == [] and not controller._macro_lock.locked()
    assert fake_engine.reset_calls == 0, "no macro was begun"


def test_the_test_button_still_runs_a_sensitive_macro(fake_engine, injector):
    controller = armed(fake_engine, injector, "{Clipboard}", triggerable=False)
    injector.clipboard = "mein text"
    assert controller.run_slot(13, block=True) is None
    assert injector.buffer == "mein text"


def test_key_combinations_and_waiting_are_not_sensitive(fake_engine, injector):
    controller = armed(fake_engine, injector, "a{Ctrl+C}{Wait 1}{WaitWindow x}")
    injector.titles = ["x"]
    assert controller.trigger_slot(13) == "ok" and idle(controller)
    assert injector.calls == [("text", "a"), ("keys", "Ctrl+C")]


def test_not_allowed_comes_before_inactive(fake_engine, injector):
    controller = armed(fake_engine, injector, "{Clipboard}")
    fake_engine.active = False
    assert controller.trigger_slot(13) == "not_allowed"


# inactive

def test_inactive_while_the_engine_is_off(fake_engine, injector, listener):
    controller = armed(fake_engine, injector, listener=listener)
    fake_engine.active = False
    assert controller.trigger_slot(13) == "inactive"
    assert injector.calls == [] and listener.errors == [] and not controller._macro_lock.locked()
    fake_engine.active = True
    assert controller.trigger_slot(13) == "ok" and idle(controller)
    assert injector.buffer == "hi"


def test_inactive_while_morse_input_is_switched_off_in_the_settings(fake_engine, injector):
    controller = armed(fake_engine, injector)
    controller.apply_settings(controller.settings.replace(enabled=False))
    assert controller.trigger_slot(13) == "inactive"
    assert injector.calls == []


# broken

@pytest.mark.parametrize("macro", ["a{Loop 2}", "{Alt+Foo}", "{Scrape nur eine Marke}", "x\ud800y"])
def test_broken_when_the_macro_does_not_parse(fake_engine, injector, listener, macro):
    controller = armed(fake_engine, injector, macro, listener=listener)
    assert controller.trigger_slot(13) == "broken"
    assert injector.calls == [] and listener.errors == [] and not controller._macro_lock.locked()


def test_an_unexpected_exception_becomes_broken_and_frees_the_lock(fake_engine, injector):
    controller = armed(fake_engine, injector, "{Ctrl+C}")

    def boom(combo):
        raise RuntimeError("Injektor kaputt")

    injector.check_shortcut = boom
    assert controller.trigger_slot(13) == "broken"
    assert not controller._macro_lock.locked()


def test_a_slot_with_a_wrong_type_field_cannot_make_it_raise(fake_engine, injector):
    controller = armed(fake_engine, injector)
    controller._function_keys = FunctionKeys((*controller._function_keys.slots[:12], "kein Slot", *controller._function_keys.slots[13:]))
    assert controller.trigger_slot(13) == "broken"


def test_no_thread_means_broken_and_the_lock_is_given_back(fake_engine, injector, monkeypatch):
    controller = armed(fake_engine, injector)

    class NoThread:
        def __init__(self, *args, **kwargs): ...

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(controller_module.threading, "Thread", NoThread)
    assert controller.trigger_slot(13) == "broken"
    assert not controller._macro_lock.locked(), "nobody would release it"
    monkeypatch.undo()
    assert controller.trigger_slot(13) == "ok" and idle(controller)


def test_run_slot_also_gives_the_lock_back_when_no_thread_starts(fake_engine, injector, monkeypatch):
    controller = armed(fake_engine, injector)

    class NoThread:
        def __init__(self, *args, **kwargs): ...

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(controller_module.threading, "Thread", NoThread)
    with pytest.raises(RuntimeError):
        controller.run_slot(13)
    assert not controller._macro_lock.locked()


# busy

def test_busy_while_a_macro_runs_and_it_is_not_typed_in_between(fake_engine, injector, listener):
    controller = make_controller(fake_engine, injector, listener)
    controller._function_keys = keys_with(13, "a")
    controller._function_keys = controller._function_keys.replace_slot(
        Slot(14, trigger="Ctrl+F14", macro="b", triggerable=True)
    )
    release, inside = threading.Event(), threading.Event()
    original = injector.inject_text

    def slow(text):
        inside.set()
        release.wait(3)
        original(text)

    injector.inject_text = slow
    assert controller.trigger_slot(13) == "ok"
    assert inside.wait(3)
    assert controller.trigger_slot(14) == "busy"
    assert controller.trigger_slot(13) == "busy"
    release.set()
    assert idle(controller)
    assert injector.buffer == "a" and listener.errors == []
    injector.inject_text = original
    assert controller.trigger_slot(14) == "ok" and idle(controller)
    assert injector.buffer == "ab"


def test_busy_while_a_morse_letter_or_the_test_button_holds_the_lock(fake_engine, injector):
    controller = armed(fake_engine, injector)
    assert controller._macro_lock.acquire(blocking=False)
    try:
        assert controller.trigger_slot(13) == "busy"
        assert controller.run_slot(13, block=True) == "Ein anderes Makro läuft noch."
    finally:
        controller._macro_lock.release()
    assert injector.calls == []
    assert controller.trigger_slot(13) == "ok" and idle(controller)


# one snapshot

def versions_of(controller, monkeypatch, *versions: FunctionKeys) -> list[int]:
    """Every read of ``controller._function_keys`` hands out the next version; returns the read counter."""
    reads = [0]

    def get(_self):
        reads[0] += 1
        return versions[min(reads[0], len(versions)) - 1]

    monkeypatch.setattr(MorseController, "_function_keys", property(get, lambda _self, value: None), raising=False)
    return reads


def test_a_flag_switched_on_after_the_check_cannot_run_a_non_triggerable_macro(fake_engine, injector, monkeypatch):
    controller = make_controller(fake_engine, injector)
    off = keys_with(13, "geheim", triggerable=False)
    on = keys_with(13, "geheim", triggerable=True)
    reads = versions_of(controller, monkeypatch, off, on)
    assert controller.trigger_slot(13) == "not_allowed"
    assert reads == [1], "the table is read once: a second read would have seen the flag on"
    assert injector.calls == []


def test_the_macro_that_runs_is_the_one_the_flag_was_checked_for(fake_engine, injector, monkeypatch):
    controller = make_controller(fake_engine, injector)
    checked = keys_with(13, "harmlos", triggerable=True)
    swapped = keys_with(13, "{Clipboard}", triggerable=False)
    reads = versions_of(controller, monkeypatch, checked, swapped)
    injector.clipboard = "geheim"
    assert controller.trigger_slot(13) == "ok" and idle(controller)
    assert reads == [1]
    assert injector.buffer == "harmlos"


def test_a_sensitive_macro_swapped_in_after_the_flag_check_is_still_refused(fake_engine, injector, monkeypatch):
    # the sensitive check reads the same slot object as the flag check, so there is no second read to swap
    controller = make_controller(fake_engine, injector)
    reads = versions_of(controller, monkeypatch, keys_with(13, "{Clipboard}"), keys_with(13, "harmlos"))
    assert controller.trigger_slot(13) == "not_allowed"
    assert reads == [1] and injector.calls == []


# run_slot keeps its behaviour

def test_run_slot_reasons_are_unchanged(fake_engine, injector):
    controller = make_controller(fake_engine, injector)
    controller._function_keys = keys_with(14, "b", enabled=False)
    assert controller.run_slot(20, block=True) == "F20 ist ausgeschaltet oder hat kein Makro."
    assert controller.run_slot(14, block=True) == "F14 ist ausgeschaltet oder hat kein Makro."
    controller._function_keys = keys_with(13, "a{Loop 2}")
    assert "EndLoop" in controller.run_slot(13, block=True)
    controller._function_keys = keys_with(13, "a")
    fake_engine.active = False
    assert controller.run_slot(13, block=True) == "Die Morse-Eingabe ist ausgeschaltet."
    assert injector.calls == []


def test_run_slot_ignores_the_triggerable_flag(fake_engine, injector):
    controller = armed(fake_engine, injector, "x", triggerable=False)
    assert controller.run_slot(13, block=True) is None
    assert injector.buffer == "x"
    assert controller.BUSY_REASON == "Ein anderes Makro läuft noch."


# stopping

def test_inactive_once_the_controller_is_stopping_or_stopped(fake_engine, injector, listener):
    controller = armed(fake_engine, injector, listener=listener)
    controller.stop()  # the engine fake keeps ``active`` true: only the controller's own state says "stopped"
    assert controller.trigger_slot(13) == "inactive"
    assert injector.calls == [] and listener.errors == [] and not controller._macro_lock.locked()


def test_not_allowed_still_comes_before_inactive_after_stop(fake_engine, injector):
    controller = armed(fake_engine, injector, "{Clipboard}")
    controller.stop()
    assert controller.trigger_slot(13) == "not_allowed"


def test_a_macro_over_the_length_cap_is_not_allowed_without_being_parsed(fake_engine, injector, monkeypatch):
    controller = armed(fake_engine, injector, "a" * (MAX_MACRO_LENGTH + 1))
    parsed = []
    monkeypatch.setattr(controller_module.macros, "parse", lambda *a: parsed.append(a) or [])
    assert controller.trigger_slot(13) == "not_allowed"
    assert parsed == [] and injector.calls == []
