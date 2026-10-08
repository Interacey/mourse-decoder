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
        self.block_input = False
        self.active = True
        self.block_shortcut = ""
        self.callback: Callable[[str, str], None] | None = None
        self.configure_calls: list[tuple[int, int, int]] = []
        self.reset_calls = 0
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

    def inject_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("Injektion kaputt")
        self.calls.append(("text", text))
        self.buffer += text

    def inject_backspaces(self, count: int) -> None:
        self.calls.append(("backspace", count))
        self.buffer = self.buffer[: max(0, len(self.buffer) - count)]


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
