"""The saved "block normal input" setting must reach the engine after startup and resume."""

from __future__ import annotations

import pytest
from mourse_decoder._native import NATIVE_AVAILABLE, load_native
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.settings import Settings


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(engine, injector, clock=None, **settings):
    kwargs = {"clock": clock} if clock else {}
    return MorseController(engine, injector, DictionaryManager(), Settings(**settings), use_hook=False, **kwargs)


def test_start_applies_saved_blocking(fake_engine, injector):
    c = make(fake_engine, injector, block_input=True)
    c.start()
    assert fake_engine.block_input is True and fake_engine.active is True


def test_blocking_is_written_again_after_the_hook_is_up(fake_engine, injector):
    """Whatever resets the engine's flag while it starts must not win over the saved setting."""
    original = fake_engine.start

    def start(callback, use_hook=True):
        original(callback, use_hook)
        fake_engine.block_input = False  # the engine comes up with its own default

    fake_engine.start = start
    c = make(fake_engine, injector, block_input=True)
    c.start()
    assert fake_engine.block_input is True


def test_stop_does_not_change_the_saved_setting(fake_engine, injector):
    c = make(fake_engine, injector, block_input=True)
    c.start()
    c.stop()
    assert fake_engine.block_input is False
    assert c.settings.block_input is True
    assert c.poll_active_toggle() is None  # nothing to save from a stopped controller
    c.start()
    assert fake_engine.block_input is True


def test_watchdog_heals_a_lost_blocking_flag(fake_engine, injector):
    clock = Clock()
    c = make(fake_engine, injector, clock, block_input=True)
    c.start()
    assert c.check_input_state() is False
    fake_engine.block_input = False
    clock.now += 0.25
    assert c.check_input_state() is True
    assert fake_engine.block_input is True
    assert c.check_input_state() is False


def test_watchdog_rewrites_state_after_a_long_gap(fake_engine, injector):
    clock = Clock()
    c = make(fake_engine, injector, clock, block_input=True, block_shortcut="Ctrl+Alt+F9")
    c.start()
    fake_engine.block_shortcut = ""  # state a resume could have lost
    clock.now += 3600
    assert c.check_input_state() is True
    assert fake_engine.block_shortcut == "Ctrl+Alt+F9"
    clock.now += 0.25
    assert c.check_input_state() is False


def test_watchdog_leaves_active_alone(fake_engine, injector):
    """The on/off shortcut flips ``active`` in the hook; the watchdog must not undo that."""
    clock = Clock()
    c = make(fake_engine, injector, clock, block_input=True)
    c.start()
    fake_engine.active = False
    fake_engine.block_input = False  # what the shortcut mirror will write next
    c.check_input_state()
    assert fake_engine.active is False


def test_watchdog_does_nothing_while_stopped(fake_engine, injector):
    c = make(fake_engine, injector, block_input=True)
    assert c.check_input_state() is False
    assert fake_engine.block_input is False


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust module not built")
def test_real_engine_state_after_load_matches_toggle_state(injector):
    native = load_native()
    engine = native.MorseEngine(trigger="ScrollLock")
    c = MorseController(engine, injector, DictionaryManager(), Settings(block_input=True), use_hook=False)
    c.start()
    try:
        loaded = (engine.block_input, engine.active)
        c.apply_settings(c.settings.replace(block_input=False))
        assert engine.block_input is False
        c.apply_settings(c.settings.replace(block_input=True))
        assert (engine.block_input, engine.active) == loaded == (True, True)
        engine.block_input = False  # lost behind the settings' back
        assert c.check_input_state() is True
        assert engine.block_input is True
    finally:
        c.stop()
