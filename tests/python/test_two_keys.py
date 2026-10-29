"""Separate dot and dash keys, and key combinations as Morse keys."""

from __future__ import annotations

import pytest
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.settings import Settings, is_valid_binding, same_binding


@pytest.mark.parametrize("spec", ["ScrollLock", "CapsLock+VK81", "caps + vk81", "Strg+Umschalt+F9", "Ctrl+Mouse5"])
def test_valid_bindings(spec):
    assert is_valid_binding(spec)


@pytest.mark.parametrize("spec", ["", " ", "CapsLock+", "Hyper+F9", "CapsLock+Nonsense", None, 7])
def test_invalid_bindings(spec):
    assert not is_valid_binding(spec)


def test_same_binding_ignores_case_blanks_and_modifier_order():
    assert same_binding("Ctrl+Shift+F9", "shift + ctrl + f9")
    assert not same_binding("Ctrl+F9", "F9")
    assert not same_binding("F9", "F10")


def test_defaults_keep_one_key_mode():
    s = Settings()
    assert (s.separate_dash_key, s.dash_trigger, s.engine_dash_trigger) == (False, "Pause", "")
    assert s.validate() == []


def test_separate_keys_must_differ():
    errors = Settings(trigger="Ctrl+Shift+F11", separate_dash_key=True, dash_trigger="shift+ctrl+F11").validate()
    assert errors == ["Punkt- und Strich-Taste müssen verschieden sein."]
    # with one key the unused dash key may be anything valid
    assert Settings(trigger="F11", dash_trigger="F11").validate() == []


@pytest.mark.parametrize(
    "changes",
    [{"dash_trigger": "Hyper+Q"}, {"separate_dash_key": "ja"}, {"trigger": "CapsLock+"}],
)
def test_broken_key_settings_are_rejected(changes):
    assert Settings(**changes).validate()


def test_old_settings_file_without_dash_key_still_loads():
    s = Settings.from_dict({"trigger": "MouseLeft", "letter_gap_ms": 900})
    assert s.separate_dash_key is False and s.validate() == []


def make(fake_engine, injector, **settings):
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings(**settings), use_hook=False)
    controller.start()
    return controller


def test_dash_key_reaches_the_engine_only_when_switched_on(fake_engine, injector):
    controller = make(fake_engine, injector, trigger="CapsLock+VK81", dash_trigger="CapsLock+VK87")
    assert (fake_engine.trigger, fake_engine.dash_trigger) == ("CapsLock+VK81", "")
    controller.apply_settings(controller.settings.replace(separate_dash_key=True))
    assert fake_engine.dash_trigger == "CapsLock+VK87"
    controller.apply_settings(controller.settings.replace(separate_dash_key=False))
    assert fake_engine.dash_trigger == ""


def test_invalid_key_settings_change_nothing(fake_engine, injector):
    controller = make(fake_engine, injector, trigger="F11")
    with pytest.raises(ValueError, match="verschieden"):
        controller.apply_settings(controller.settings.replace(separate_dash_key=True, dash_trigger="F11"))
    assert controller.settings.separate_dash_key is False
    assert fake_engine.dash_trigger == ""
