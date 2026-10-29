"""Tests for the blocking setting, the shortcut format and the controller wiring."""

from __future__ import annotations

import pytest
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.settings import Settings, is_valid_shortcut
from mourse_decoder.triggers import binding_label


@pytest.mark.parametrize(
    "spec", ["", "Pause", "Ctrl+Shift+Pause", "ctrl + alt + VK77", "Win+F9", "Ctrl+Mouse5", "CapsLock+VK81"]
)
def test_valid_shortcuts(spec):
    assert is_valid_shortcut(spec)


@pytest.mark.parametrize("spec", ["Ctrl+", "Hyper+F9", "Ctrl+Nonsense", "+Pause", None, 5])
def test_invalid_shortcuts(spec):
    assert not is_valid_shortcut(spec)


def test_settings_defaults_and_validation():
    s = Settings()
    assert s.block_input is False and s.block_shortcut == "Ctrl+Shift+Pause"
    assert s.validate() == []
    assert Settings(block_input="yes").validate()
    assert Settings(block_shortcut="Ctrl+").validate()


def test_binding_labels():
    assert binding_label("Ctrl+Shift+Pause") == "Ctrl + Shift + Pause"
    assert binding_label("Alt+VK77") == "Alt + M"
    assert binding_label("CapsLock+VK81") == "Caps Lock + Q"
    assert binding_label("ScrollLock") == "Scroll Lock"
    assert binding_label("") == ""


def make(fake_engine, injector, **settings):
    return MorseController(fake_engine, injector, DictionaryManager(), Settings(**settings), use_hook=False)


def test_blocking_and_active_state_are_pushed_to_the_engine(fake_engine, injector):
    controller = make(fake_engine, injector, block_input=True)
    controller.start()
    assert fake_engine.block_input is True and fake_engine.active is True
    assert fake_engine.block_shortcut == "Ctrl+Shift+Pause"
    controller.apply_settings(controller.settings.replace(enabled=False))
    assert fake_engine.active is False  # the engine then lets the key through
    controller.apply_settings(controller.settings.replace(enabled=True))
    assert fake_engine.active is True
    controller.stop()
    assert fake_engine.block_input is False


def test_shortcut_toggle_flips_both_switches(fake_engine, injector):
    controller = make(fake_engine, injector, block_input=True)
    controller.start()
    assert controller.poll_active_toggle() is None
    fake_engine.active = False  # the hook flipped it
    toggled = controller.poll_active_toggle()
    assert toggled is not None and (toggled.enabled, toggled.block_input) == (False, False)
    controller.apply_settings(toggled)
    assert fake_engine.block_input is False
    assert controller.poll_active_toggle() is None
    fake_engine.active = True
    back = controller.poll_active_toggle()
    assert (back.enabled, back.block_input) == (True, True)  # both come back on together


def test_morse_output_stops_while_the_shortcut_has_it_off(fake_engine, injector):
    controller = make(fake_engine, injector)
    controller.start()
    fake_engine.letters(".-")
    assert injector.buffer == "A"
    controller.apply_settings(controller.settings.replace(enabled=False))
    fake_engine.letters("-...")
    assert injector.buffer == "A"  # nothing typed while off
    controller.apply_settings(controller.settings.replace(enabled=True))
    fake_engine.letters("-...")
    assert injector.buffer == "AB"


def test_no_poll_result_when_not_running(fake_engine, injector):
    controller = make(fake_engine, injector)
    fake_engine.active = False
    assert controller.poll_active_toggle() is None
