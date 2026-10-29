"""Long output texts and key combinations ({Ctrl+C}) in smiley sets."""

from __future__ import annotations

import json
import sys

import pytest
from mourse_decoder import clipboard
from mourse_decoder.controller import MorseController, NativeInjector
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.macros import expand_loops
from mourse_decoder.settings import Settings

LONG = "Lorem ipsum dolor sit amet.\n" * 1000  # 28000 characters


def make_controller(tmp_path, engine, injector, codes):
    (tmp_path / "macros.json").write_text(json.dumps({"id": "macros", "codes": codes}), encoding="utf-8")
    dictionaries = DictionaryManager(smiley_directory=tmp_path)
    controller = MorseController(engine, injector, dictionaries, Settings().replace(smileys="macros"))
    controller.start()
    return controller


def test_long_text_is_typed_in_full(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {"--..--..": LONG})
    fake_engine.letters("--..--..")
    assert injector.buffer == LONG
    assert controller.typed_text == LONG[-MorseController.HISTORY_LIMIT :]  # history is capped on purpose


def test_several_shortcuts_in_a_row(tmp_path, fake_engine, injector):
    make_controller(tmp_path, fake_engine, injector, {"--..--..": "{Ctrl+A}{Ctrl+C}{Ctrl+End}{Ctrl+V}"})
    fake_engine.letters("--..--..")
    assert injector.calls == [("keys", "Ctrl+A"), ("keys", "Ctrl+C"), ("keys", "Ctrl+End"), ("keys", "Ctrl+V")]


def test_text_and_shortcuts_mixed_in_order(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {"--..--..": "Hallo{Enter}Welt{Ctrl+S}"})
    fake_engine.letters("--..--..")
    assert injector.calls == [("text", "Hallo"), ("keys", "Enter"), ("text", "Welt"), ("keys", "Ctrl+S")]
    assert controller.typed_text == "HalloWelt"  # keys are no typed characters


@pytest.fixture
def record_waits(monkeypatch, injector):
    monkeypatch.setattr(MorseController, "_sleep", lambda self, s: injector.calls.append(("wait", s)))


def test_wait_pauses_between_steps(tmp_path, fake_engine, injector, record_waits):
    make_controller(tmp_path, fake_engine, injector, {"--..--..": "{Win+R}{Wait 500}firefox{Enter}{ wait300 }"})
    fake_engine.letters("--..--..")
    assert injector.calls == [
        ("keys", "Win+R"),
        ("wait", 0.5),
        ("text", "firefox"),
        ("keys", "Enter"),
        ("wait", 0.3),
    ]


def test_wait_window_types_only_once_the_window_is_in_front(tmp_path, fake_engine, injector, monkeypatch):
    import mourse_decoder.controller as controller_module

    monkeypatch.setattr(controller_module.time, "sleep", lambda s: injector.calls.append(("poll", s)))
    # the old Notepad tab is in front first, the new "Untitled" tab only on the third look
    injector.titles = ["Run", "notes.txt - Notepad", "Untitled - Notepad"]
    make_controller(tmp_path, fake_engine, injector, {"--..--..": "{Win+R}notepad{Enter}{WaitWindow untitled}Hallo"})
    fake_engine.letters("--..--..")
    assert injector.calls == [
        ("keys", "Win+R"),
        ("text", "notepad"),
        ("keys", "Enter"),
        ("poll", 0.05),
        ("poll", 0.05),
        ("text", "Hallo"),
    ]


def test_wait_window_timeout_aborts_the_rest(tmp_path, fake_engine, injector, listener, monkeypatch):
    monkeypatch.setattr(MorseController, "WINDOW_TIMEOUT_S", 0.05)
    injector.titles = ["Minecraft"]
    controller = make_controller(tmp_path, fake_engine, injector, {"--..--..": "{Ctrl+N}{WaitWindow Notepad}Hallo"})
    controller.set_listener(listener)
    fake_engine.letters("--..--..")
    assert injector.calls == [("keys", "Ctrl+N")]  # "Hallo" never typed into Minecraft
    assert listener.errors and "Notepad" in listener.errors[0]


def test_loop_repeats_text_keys_and_waits(tmp_path, fake_engine, injector, record_waits):
    make_controller(tmp_path, fake_engine, injector, {"--..--..": "{Loop 2}Hi{Enter}{Wait 100}{EndLoop}Ende"})
    fake_engine.letters("--..--..")
    assert injector.calls == [
        ("text", "Hi"), ("keys", "Enter"), ("wait", 0.1),
        ("text", "Hi"), ("keys", "Enter"), ("wait", 0.1),
        ("text", "Ende"),
    ]


@pytest.mark.parametrize(
    ("macro", "expected"),
    [
        ("{Loop 3}ab{EndLoop}", "ababab"),
        ("{loop 2}x{LOOP 3}y{endloop}{EndLoop}", "xyyyxyyy"),  # nested, any case
        ("{Loop 2}A{EndLoop}-{Loop 2}B{EndLoop}", "AA-BB"),  # one after another
        ("{ Loop 1 }\n{ EndLoop }", "\n"),
        ("{Loop 0}weg{EndLoop}da", "da"),
        ("ohne Schleife", "ohne Schleife"),
    ],
)
def test_expand_loops(macro, expected):
    assert expand_loops(macro) == expected


@pytest.mark.parametrize(
    "macro",
    [
        "{Loop 2}offen",  # no {EndLoop}
        "zu{EndLoop}",
        "{Loop 2}{Loop 2}x{EndLoop}",
        "{Loop 1001}x{EndLoop}",  # above the limit
        "{Loop 1000}{Loop 1000}xx{EndLoop}{EndLoop}",  # 2 million characters
    ],
)
def test_broken_loops_abort_before_anything_is_typed(tmp_path, fake_engine, injector, listener, macro):
    controller = make_controller(tmp_path, fake_engine, injector, {"--..--..": "vorher" + macro})
    controller.set_listener(listener)
    fake_engine.letters("--..--..")
    assert injector.calls == []
    assert len(listener.errors) == 1


# --- protection against operating mistakes ---


@pytest.mark.parametrize("macro", ["Hallo{Crtl+C}", "{Win+R}{Entr}", "{Wiat 500}x", "{Loop 2}a{EndLoop}{Foo}"])
def test_typo_in_a_command_stops_the_macro_before_anything_happens(
    tmp_path, fake_engine, injector, listener, macro
):
    controller = make_controller(tmp_path, fake_engine, injector, {"--..--..": macro})
    controller.set_listener(listener)
    fake_engine.letters("--..--..")
    assert injector.calls == []
    assert len(listener.errors) == 1 and "Nichts getippt" in listener.errors[0]


def test_german_command_names(tmp_path, fake_engine, injector, record_waits):
    injector.titles = ["Editor"]
    make_controller(
        tmp_path, fake_engine, injector,
        {"--..--..": "{Schleife 2}a{SchleifenEnde}{Warte 100}{WarteFenster editor}b"},
    )
    fake_engine.letters("--..--..")
    assert injector.calls == [("text", "aa"), ("wait", 0.1), ("text", "b")]


def test_switching_morse_off_stops_a_long_wait(tmp_path, fake_engine, injector, listener, monkeypatch):
    import mourse_decoder.controller as controller_module

    # the user hits the on/off shortcut while the macro waits
    monkeypatch.setattr(controller_module.time, "sleep", lambda s: setattr(fake_engine, "active", False))
    controller = make_controller(tmp_path, fake_engine, injector, {"--..--..": "vorher{Wait 99999}danach"})
    controller.set_listener(listener)
    fake_engine.letters("--..--..")
    assert injector.calls == [("text", "vorher")]
    assert "ausgeschaltet" in listener.errors[0]


def test_switching_morse_off_stops_a_running_loop(tmp_path, fake_engine, injector, listener):
    controller = make_controller(tmp_path, fake_engine, injector, {"--..--..": "{Loop 1000}x{Ctrl+C}{EndLoop}"})
    controller.set_listener(listener)
    press = injector.press_shortcut

    def press_then_switch_off(combo):
        press(combo)
        fake_engine.active = False

    injector.press_shortcut = press_then_switch_off
    fake_engine.letters("--..--..")
    assert injector.calls == [("text", "x"), ("keys", "Ctrl+C")]
    assert len(listener.errors) == 1


def test_clicks_during_a_macro_are_dropped(tmp_path, fake_engine, injector):
    make_controller(tmp_path, fake_engine, injector, {"--..--..": "{Ctrl+C}"})
    fake_engine.letters(".-")
    assert fake_engine.reset_calls == 0  # plain letters don't reset
    fake_engine.letters("--..--..")
    assert fake_engine.reset_calls == 1


def test_braces_that_are_no_shortcut_are_typed(tmp_path, fake_engine, injector):
    make_controller(tmp_path, fake_engine, injector, {"--..--..": "{^_^} { } {"})
    fake_engine.letters("--..--..")
    assert injector.buffer == "{^_^} { } {"


def test_letters_still_work_next_to_macros(tmp_path, fake_engine, injector):
    make_controller(tmp_path, fake_engine, injector, {"--..--..": "{Ctrl+C}"})
    fake_engine.letters(".-", "--..--..", "-...")
    assert injector.calls == [("text", "A"), ("keys", "Ctrl+C"), ("text", "B")]


class FakeNative:
    def __init__(self, clipboard_fake=None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._clipboard = clipboard_fake

    def inject_text(self, text):
        self.calls.append(("text", text))

    def inject_backspaces(self, count):
        self.calls.append(("backspace", count))

    def press_shortcut(self, combo):
        # what the app sees at the moment Ctrl+V arrives
        self.calls.append(("keys", combo, self._clipboard.content if self._clipboard else None))


class FakeClipboard:
    """``content`` stands for everything on the clipboard (text, HTML, image, ...)."""

    def __init__(self, content="alt", busy=False, on_windows=True) -> None:
        self.content = content
        self.busy = busy
        self.on_windows = on_windows

    def available(self):
        return self.on_windows

    def save(self):
        if self.busy:
            raise OSError("belegt")
        return [("all", self.content)]

    def restore(self, saved):
        [(_, self.content)] = saved

    def set_text(self, text):
        self.content = text


@pytest.fixture
def no_settle(monkeypatch):
    monkeypatch.setattr(NativeInjector, "PASTE_SETTLE_S", 0)


def test_long_text_is_pasted_and_clipboard_restored(no_settle):
    board = FakeClipboard()
    native = FakeNative(board)
    NativeInjector(native, board).inject_text("a\nb" * 100)
    assert native.calls == [("keys", "Ctrl+V", "a\r\nb" * 100)]
    assert board.content == "alt"


def test_short_text_is_typed():
    board = FakeClipboard()
    native = FakeNative(board)
    NativeInjector(native, board).inject_text("x" * (NativeInjector.PASTE_FROM - 1))
    assert native.calls == [("text", "x" * (NativeInjector.PASTE_FROM - 1))]


def test_formatted_clipboard_content_is_pasted_over_and_restored(no_settle):
    # regression: with HTML from a web page on the clipboard the text was typed, and Notepad
    # dropped all but the first characters
    board = FakeClipboard(content={"html": "<b>NIV</b>", "text": "NIV"})
    native = FakeNative(board)
    NativeInjector(native, board).inject_text(LONG)
    assert native.calls == [("keys", "Ctrl+V", LONG.replace("\n", "\r\n"))]
    assert board.content == {"html": "<b>NIV</b>", "text": "NIV"}


@pytest.mark.parametrize("board", [FakeClipboard(busy=True), FakeClipboard(on_windows=False)])
def test_long_text_is_typed_when_clipboard_is_unusable(board):
    native = FakeNative(board)
    NativeInjector(native, board).inject_text(LONG)
    assert native.calls == [("text", LONG)]
    assert board.content == "alt"


def test_clipboard_is_restored_even_if_paste_fails(no_settle):
    board = FakeClipboard()

    class Broken(FakeNative):
        def press_shortcut(self, combo):
            raise RuntimeError("kaputt")

    with pytest.raises(RuntimeError):
        NativeInjector(Broken(), board).inject_text(LONG)
    assert board.content == "alt"


def test_empty_clipboard_stays_empty(no_settle):
    board = FakeClipboard(content=None)
    NativeInjector(FakeNative(board), board).inject_text(LONG)
    assert board.content is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-Zwischenablage")
def test_real_clipboard_keeps_all_formats():
    import ctypes

    users = clipboard.save()
    try:
        html_format = ctypes.windll.user32.RegisterClipboardFormatW("HTML Format")
        html = b"Version:0.9\r\n<b>NIV</b>\x00"
        with clipboard._Open():
            ctypes.windll.user32.EmptyClipboard()
            clipboard._put(clipboard.CF_UNICODETEXT, "NIV\0".encode("utf-16-le"))
            clipboard._put(html_format, html)
        saved = clipboard.save()

        text = "Grüße 😀 中文\r\n" * 5000
        clipboard.set_text(text)
        assert clipboard.get_text() == text

        clipboard.restore(saved)
        assert clipboard.get_text() == "NIV"
        restored = dict(clipboard.save())
        assert restored[html_format].startswith(html)  # GlobalSize may round up
    finally:
        clipboard.restore(users)
