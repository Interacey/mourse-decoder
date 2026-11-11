"""Shared test helpers (fakes and fixtures).

The fakes implement the same protocols as the real engine and injector and record
every call, so the controller can be tested without Rust, a display or real typing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from mourse_decoder.telecode import build_database


class FakeEngine:
    """Stands in for ``_morse_core.MorseEngine``; events are fired with ``emit``."""

    def __init__(self) -> None:
        self.trigger = "ScrollLock"
        self.dash_trigger = ""
        self.block_input = False
        self.active = True
        self.block_shortcut = ""
        self.cycle_shortcut = ""
        self.cycle_count = 0
        self.undo_shortcut = ""
        self.undo_count = 0
        self.function_keys: list[tuple[int, str, bool]] = []
        self.fired_slots: list[int] = []  # slot numbers ``poll_function_key`` hands out
        self.modifiers_released = True
        self.abort_requested = False
        self.set_function_keys_error: str | None = None
        self.callback: Callable[[str, str], None] | None = None
        self.configure_calls: list[tuple[int, int, int]] = []
        self.reset_calls = 0
        self.measuring = False
        self.measuring_calls: list[bool] = []
        self.hold_acks = False  # True: the "measuring" marker stays in ``pending_acks`` (a worker that lags)
        self.pending_acks: list[str] = []
        self.started = False
        self.use_hook: bool | None = None

    def start(self, callback, use_hook: bool = True) -> None:
        if self.started:
            raise RuntimeError("läuft bereits")
        self.callback = callback
        self.use_hook = use_hook
        self.started = True

    def stop(self) -> None:
        self.started = False

    def configure(self, dot_threshold_ms: int, letter_gap_ms: int, word_gap_ms: int = 0) -> None:
        self.configure_calls.append((dot_threshold_ms, letter_gap_ms, word_gap_ms))

    def reset(self) -> None:
        self.reset_calls += 1

    def set_measuring(self, on: bool) -> None:
        self.measuring = on
        self.measuring_calls.append(on)
        if self.started:  # the real worker resets itself and answers with a marker at the place of the switch
            self.reset_calls += 1
            self.pending_acks.append("1" if on else "0")
            if not self.hold_acks:
                self.release_acks()

    def release_acks(self) -> None:
        while self.pending_acks:
            self.emit("measuring", self.pending_acks.pop(0))

    def set_function_keys(self, entries: list[tuple[int, str, bool]]) -> None:
        if self.set_function_keys_error:
            raise ValueError(self.set_function_keys_error)
        self.function_keys = list(entries)

    def clear_function_keys(self) -> None:
        self.function_keys = []

    def poll_function_key(self) -> int | None:
        return self.fired_slots.pop(0) if self.fired_slots else None

    def function_key_modifiers_released(self) -> bool:
        return self.modifiers_released

    def poll_abort(self) -> bool:
        aborted, self.abort_requested = self.abort_requested, False
        return aborted

    # test api
    def emit(self, kind: str, payload: str = "") -> None:
        assert self.callback is not None, "Engine nicht gestartet"
        self.callback(kind, payload)

    def letters(self, *sequences: str) -> None:
        for seq in sequences:
            for symbol in seq:
                self.emit("symbol", symbol)
            self.emit("letter", seq)


class RecordingInjector:
    """Simulates a text field: backspaces really delete."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.buffer = ""
        self.fail = False
        self.titles = [""]
        self.clipboard: str | None = None  # what ``clipboard_text`` returns
        self.delays: list[int] = []  # every ``set_typing_delay`` call; the last one is the pause in force
        self.typed_with: list[tuple[str, int | None]] = []  # each typed text and the pause in force for it

    def inject_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("Injektion kaputt")
        self.calls.append(("text", text))
        self.typed_with.append((text, self.delays[-1] if self.delays else None))
        self.buffer += text

    def type_text(self, text: str) -> None:
        self.inject_text(text)

    def set_typing_delay(self, ms: int) -> None:
        self.delays.append(ms)

    def pastes(self, text: str) -> bool:
        return False

    def inject_backspaces(self, count: int) -> None:
        self.calls.append(("backspace", count))
        self.buffer = self.buffer[: max(0, len(self.buffer) - count)]

    def check_shortcut(self, combo: str) -> None:
        # rough stand-in for the Rust parser: known modifiers, then one key
        *modifiers, key = (part.strip().lower() for part in combo.split("+"))
        known_key = (len(key) == 1 and key.isalnum()) or key in {"enter", "end", "esc", "tab", "delete", "f5", "backspace", "space"}
        if any(m not in {"ctrl", "strg", "alt", "shift", "win"} for m in modifiers) or not known_key:
            raise ValueError(f"keine Tastenkombination: {combo!r}")

    def press_shortcut(self, combo: str) -> None:
        self.check_shortcut(combo)
        self.calls.append(("keys", combo))

    def foreground_title(self) -> str:
        # tests set ``titles`` to the window titles seen one after another
        return self.titles.pop(0) if len(self.titles) > 1 else self.titles[0]

    def clipboard_text(self) -> str | None:
        return self.clipboard

    def paste_files(self, paths: list[str]) -> None:
        self.calls.append(("files", paths))

    def scroll(self, notches: int) -> None:
        self.calls.append(("scroll", notches))


class RecordingListener:
    def __init__(self) -> None:
        self.symbols: list[str] = []
        self.letters: list[tuple[str, str | None]] = []
        self.errors: list[str] = []

    def on_symbol(self, symbol: str) -> None:
        self.symbols.append(symbol)

    def on_letter(self, sequence: str, text: str | None) -> None:
        self.letters.append((sequence, text))

    def on_error(self, message: str) -> None:
        self.errors.append(message)


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def injector() -> RecordingInjector:
    return RecordingInjector()


@pytest.fixture
def listener() -> RecordingListener:
    return RecordingListener()


@pytest.fixture
def alphabet_dir(tmp_path: Path) -> Callable[..., Path]:
    """Factory that writes alphabet files into a temp directory.

    Usage: ``alphabet_dir(base={"id": "base", "codes": {...}}, ...)``. Strings are
    written as is, which allows broken JSON.
    """

    def write(**files: dict | str) -> Path:
        for name, content in files.items():
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            (tmp_path / f"{name}.json").write_text(text, encoding="utf-8")
        return tmp_path

    return write


# Sample data in Unihan format. 0022 and 0948 are the known telegraph codes for
# "China"; 9999 is a deliberate duplicate.
UNIHAN_SAMPLE = """\
# comment line like in the real file
U+4E2D\tkMainlandTelegraph\t0022
U+56FD\tkMainlandTelegraph\t0948
U+570B\tkTaiwanTelegraph\t0948
U+4EBA\tkMainlandTelegraph\t0086
U+4E00\tkMainlandTelegraph\t9999
U+4E8C\tkMainlandTelegraph\t9999
U+4E2D\tkJapanese\tCHUU
"""


@pytest.fixture
def telecode_db(tmp_path: Path) -> Path:
    from mourse_decoder.telecode import parse_unihan

    path = tmp_path / "telecode.sqlite"
    build_database(parse_unihan(UNIHAN_SAMPLE.splitlines()), path)
    return path


@pytest.fixture(scope="session")
def tk_root():
    """One Tk root for all UI tests; a second tk.Tk() in one process sometimes fails to initialise Tcl."""
    import tkinter as tk

    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("kein Display")
    yield window
    window.destroy()
