"""A pause by the on/off shortcut lasts until the program ends: it must never reach settings.json."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mourse_decoder._native import NATIVE_AVAILABLE, load_native
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.settings import Settings, SettingsStore

try:
    import mourse_decoder.ui.tray as tray_mod
except Exception as exc:  # no display for pystray
    pytest.skip(f"UI modules not importable here: {exc!r}", allow_module_level=True)


def make_tray(controller, store):
    """A TrayApp without Tk: only what _apply and the toggle poll touch."""
    tray = object.__new__(tray_mod.TrayApp)
    tray._controller = controller
    tray._store = store
    tray._saved_flags = {"enabled": controller.settings.enabled, "block_input": controller.settings.block_input}
    tray._icon = SimpleNamespace(icon=None, update_menu=lambda: None, title="")
    tray._window = SimpleNamespace(sync=lambda: None)
    tray._toast = SimpleNamespace(show=lambda *a: None)
    tray._set_title = lambda text: None
    tray._tooltip = lambda: ""
    return tray


def start(engine, injector, store):
    controller = MorseController(engine, injector, DictionaryManager(), store.load(), use_hook=False)
    controller.start()
    return controller, make_tray(controller, store)


def pause_by_shortcut(engine, tray):
    engine.active = False  # what the hook does when the shortcut fires
    tray._poll_block()


def test_pause_by_shortcut_is_not_saved(fake_engine, injector, tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings(block_input=True))
    controller, tray = start(fake_engine, injector, store)
    pause_by_shortcut(fake_engine, tray)
    assert (controller.settings.enabled, controller.settings.block_input) == (False, True)  # shown as paused, blocking setting untouched
    saved = store.load()
    assert (saved.enabled, saved.block_input) == (True, True)


def test_saving_another_setting_while_paused_keeps_the_pause_and_the_saved_values(fake_engine, injector, tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings(block_input=True))
    controller, tray = start(fake_engine, injector, store)
    pause_by_shortcut(fake_engine, tray)
    tray._apply(controller.settings.replace(letter_gap_ms=700))
    assert controller.settings.enabled is False and fake_engine.active is False  # still paused
    saved = store.load()
    assert saved.letter_gap_ms == 700
    assert (saved.enabled, saved.block_input) == (True, True)
    # restart from the file
    engine2 = type(fake_engine)()
    again, _ = start(engine2, injector, store)
    assert (engine2.block_input, engine2.active) == (True, True)
    assert again.settings.letter_gap_ms == 700


def test_alphabet_cycle_while_paused_does_not_save_the_pause(fake_engine, injector, tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings(block_input=True))
    controller, tray = start(fake_engine, injector, store)
    pause_by_shortcut(fake_engine, tray)
    tray._apply(controller.settings.replace(alphabet="german"))
    saved = store.load()
    assert saved.alphabet == "german" and (saved.enabled, saved.block_input) == (True, True)


def test_a_change_made_by_the_user_is_saved(fake_engine, injector, tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings(block_input=True))
    controller, tray = start(fake_engine, injector, store)
    tray._apply(controller.settings.replace(block_input=False))
    assert store.load().block_input is False
    tray._apply(controller.settings.replace(enabled=False))  # tray menu "Morse input"
    assert store.load().enabled is False
    pause_by_shortcut(fake_engine, tray)  # already off: nothing to flip, nothing saved
    tray._apply(controller.settings.replace(enabled=True))
    assert store.load().enabled is True


def test_resuming_by_shortcut_keeps_saved_values(fake_engine, injector, tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings(block_input=True))
    controller, tray = start(fake_engine, injector, store)
    pause_by_shortcut(fake_engine, tray)
    fake_engine.active = True
    tray._poll_block()
    assert (controller.settings.enabled, controller.settings.block_input) == (True, True)
    saved = store.load()
    assert (saved.enabled, saved.block_input) == (True, True)


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust module not built")
def test_real_engine_pause_save_restart(injector, tmp_path):
    native = load_native()
    store = SettingsStore(tmp_path)
    store.save(Settings(block_input=True))
    engine = native.MorseEngine(trigger="ScrollLock")
    controller, tray = start(engine, injector, store)
    try:
        assert (engine.block_input, engine.active) == (True, True)
        engine.active = False  # the shortcut's effect inside the hook
        tray._poll_block()
        assert engine.block_input is True  # the engine ANDs it with active
        tray._apply(controller.settings.replace(typing_delay_ms=30))
    finally:
        controller.stop()
    engine2 = native.MorseEngine(trigger="ScrollLock")
    controller2, _ = start(engine2, injector, store)
    try:
        assert (engine2.block_input, engine2.active) == (True, True)
        assert controller2.settings.typing_delay_ms == 30
    finally:
        controller2.stop()


# One model for every way to switch Morse input: the shortcut, the tray menu and the settings switch change
# `enabled` and nothing else. `block_input` is the saved setting at all times; the engine blocks only while
# Morse input is on, so a pause never blocks and a resume blocks exactly when the setting says so.
def by_shortcut(engine, controller, tray):
    engine.active = not engine.active  # what the hook does
    tray._poll_block()


def by_menu(engine, controller, tray):
    tray._apply(controller.settings.replace(enabled=not controller.settings.enabled))


def by_form(engine, controller, tray):
    tray._apply(controller.settings.replace(enabled=not controller.settings.enabled, letter_gap_ms=650))


WAYS = {"shortcut": by_shortcut, "menu": by_menu, "form": by_form}


@pytest.mark.parametrize("saved_block", [True, False])
@pytest.mark.parametrize("second", WAYS)
@pytest.mark.parametrize("first", WAYS)
def test_every_way_gives_the_same_state(fake_engine, injector, tmp_path, first, second, saved_block):
    store = SettingsStore(tmp_path)
    store.save(Settings(block_input=saved_block))
    controller, tray = start(fake_engine, injector, store)
    expected_saved_enabled = True
    on = True
    for way in (first, second, first, second):  # off, on, off, on again
        WAYS[way](fake_engine, controller, tray)
        on = not on
        if way != "shortcut":
            expected_saved_enabled = on
        assert controller.settings.enabled is on
        assert fake_engine.active is on
        assert controller.settings.block_input is saved_block
        assert fake_engine.block_input is saved_block
        assert controller.poll_active_toggle() is None  # the controller and the engine agree
        saved = store.load()
        assert (saved.enabled, saved.block_input) == (expected_saved_enabled, saved_block)
    engine2 = type(fake_engine)()
    again, _ = start(engine2, injector, store)
    assert (engine2.active, engine2.block_input) == (expected_saved_enabled, saved_block)
    assert again.settings.enabled is expected_saved_enabled


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust module not built")
@pytest.mark.parametrize("first,second", [("shortcut", "menu"), ("menu", "shortcut")])
def test_real_engine_shortcut_and_menu_agree(injector, tmp_path, first, second):
    native = load_native()
    store = SettingsStore(tmp_path)
    store.save(Settings(block_input=True))
    engine = native.MorseEngine(trigger="ScrollLock")
    controller, tray = start(engine, injector, store)
    try:
        for way in (first, second):
            WAYS[way](engine, controller, tray)
        assert (engine.active, engine.block_input) == (True, True)  # off, then on again
        assert (controller.settings.enabled, controller.settings.block_input) == (True, True)
        WAYS[first](engine, controller, tray)
        assert (engine.active, controller.settings.enabled) == (False, False)
        assert controller.settings.block_input is True and engine.block_input is True  # the engine ANDs both
    finally:
        controller.stop()
