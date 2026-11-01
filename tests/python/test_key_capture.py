"""The key/shortcut capture button stops waiting by itself after a while."""

from __future__ import annotations

import tkinter as tk

import pytest
from mourse_decoder.ui import key_capture
from mourse_decoder.ui.key_capture import CAPTURE_TIMEOUT_S, ShortcutCaptureButton


class FakeBackend:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.capturing = False

    def begin_capture(self) -> None:
        self.capturing = True

    def cancel_capture(self) -> None:
        self.capturing = False

    def poll_capture(self) -> str | None:
        if self.tokens:
            self.capturing = False
            return self.tokens.pop(0)
        return None


@pytest.fixture(scope="module")
def root():
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("kein Display")
    yield window
    window.destroy()


@pytest.fixture
def setup(root, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(key_capture.time, "monotonic", lambda: now[0])
    backend, changes = FakeBackend(), []
    button = ShortcutCaptureButton(root, "", backend=backend, on_change=changes.append)
    yield button, backend, changes, now
    button.destroy()


def test_waiting_ends_after_8_seconds_and_releases_the_hook(setup):
    button, backend, changes, now = setup
    button._toggle()
    assert button.waiting and backend.capturing
    now[0] += CAPTURE_TIMEOUT_S - 0.1
    button._poll()
    assert button.waiting  # still within the time
    now[0] += 0.2
    button._poll()
    assert not button.waiting and not backend.capturing
    assert button.shortcut == "" and changes == []  # it had no shortcut, so nothing changed


def test_timeout_removes_an_assigned_shortcut_completely(root, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(key_capture.time, "monotonic", lambda: now[0])
    backend, changes = FakeBackend(), []
    button = ShortcutCaptureButton(root, "Ctrl+Alt+F9", backend=backend, on_change=changes.append)
    button._toggle()
    now[0] += CAPTURE_TIMEOUT_S + 0.1
    button._poll()
    assert button.shortcut == "" and changes == [""]  # saved as "no shortcut"
    assert not button.waiting and not backend.capturing
    button.destroy()


def test_timeout_leaves_the_morse_key_alone(root, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(key_capture.time, "monotonic", lambda: now[0])
    backend, changes = FakeBackend(), []
    button = key_capture.KeyCaptureButton(root, "F8", backend=backend, on_change=changes.append)
    button._toggle()
    now[0] += CAPTURE_TIMEOUT_S + 0.1
    button._poll()
    assert button.trigger == "F8" and changes == []  # a Morse key can't be empty
    button.destroy()


def test_a_combination_within_the_time_is_taken(setup):
    button, backend, changes, now = setup
    button._toggle()
    now[0] += CAPTURE_TIMEOUT_S - 1
    backend.tokens.append("Ctrl+Alt+F9")
    button._poll()
    assert button.shortcut == "Ctrl+Alt+F9" and changes == ["Ctrl+Alt+F9"] and not button.waiting


def test_an_unusable_key_gives_a_fresh_8_seconds(setup):
    button, backend, changes, now = setup
    button._toggle()
    now[0] += CAPTURE_TIMEOUT_S - 1
    backend.tokens.append("MetaRight")  # the hook can't use it, so it keeps waiting
    button._poll()
    assert button.waiting
    now[0] += CAPTURE_TIMEOUT_S - 1
    button._poll()
    assert button.waiting
    now[0] += 2
    button._poll()
    assert not button.waiting


def test_the_button_can_be_used_again_after_a_timeout(setup):
    button, backend, changes, now = setup
    button._toggle()
    now[0] += CAPTURE_TIMEOUT_S + 1
    button._poll()
    now[0] += 5
    button._toggle()
    assert button.waiting
    assert button._deadline == now[0] + CAPTURE_TIMEOUT_S
