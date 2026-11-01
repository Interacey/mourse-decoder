"""Special cases: many line breaks, braces, the clipboard macro and the file macro."""

from __future__ import annotations

import ctypes
import struct
import sys

import pytest
from mourse_decoder import clipboard, macros
from mourse_decoder.controller import MorseController, NativeInjector
from mourse_decoder.smileys import load_smiley_codes
from test_macros import FakeClipboard, FakeNative, make_controller

CODE = "--..--.."
MANY_BREAKS = "Hallo\n\n\n\n\ndu \n\n\nhaha"


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(NativeInjector, "PASTE_SETTLE_S", 0)
    monkeypatch.setattr(NativeInjector, "PASTE_FILES_SETTLE_S", 0)


# --- macro parsing ---


@pytest.mark.parametrize(
    ("macro", "steps"),
    [
        (MANY_BREAKS, [("text", MANY_BREAKS)]),  # the reported case: stays one piece
        ("\n\n{Enter}\n\n", [("text", "\n\n"), ("keys", "Enter"), ("text", "\n\n")]),
        ("a\r\nb\rc", [("text", "a\r\nb\rc")]),
        ("{}", [("text", "{}")]),
        ("a } b { c", [("text", "a } b { c")]),
        ("{{Enter}}", [("text", "{"), ("keys", "Enter"), ("text", "}")]),  # inner braces win
        ("{Enter}{Enter}", [("keys", "Enter"), ("keys", "Enter")]),
        ("{Wait 0}", [("wait", 0.0)]),
        ("{Clipboard}", [("clipboard", None)]),
        ("x{ ZWISCHENABLAGE }{enter}", [("text", "x"), ("clipboard", None), ("keys", "enter")]),
        ("{File C:\\a.txt}", [("files", ["C:\\a.txt"])]),
        ('{Datei "C:\\a b\\c.txt" | D:\\x }', [("files", ["C:\\a b\\c.txt", "D:\\x"])]),
        ("{ordner %TEMP%}", [("files", ["%TEMP%"])]),
        ("{Loop 2}{File x}{EndLoop}", [("files", ["x"]), ("files", ["x"])]),
    ],
)
def test_parse_special_cases(injector, macro, steps):
    assert macros.parse(macro, injector.check_shortcut) == steps


@pytest.mark.parametrize("macro", ["{File |}", "{File a||b}", "{Datei }"])
def test_empty_paths_are_rejected(injector, macro):
    with pytest.raises(macros.MacroError):
        macros.parse(macro, injector.check_shortcut)


def test_many_line_breaks_reach_the_injector_in_one_piece(tmp_path, fake_engine, injector):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: MANY_BREAKS})
    fake_engine.letters(CODE)
    assert injector.calls == [("text", MANY_BREAKS)]
    assert controller.typed_text == MANY_BREAKS


def test_raw_line_breaks_in_a_hand_written_set_survive(tmp_path):
    # pasted into the file with Windows line endings and a blank line in the middle
    (tmp_path / "s.json").write_bytes(b'{"id": "s", "codes": {"--..--..": "Hallo\r\n\r\ndu\r\n\r\n\r\nhaha"}}')
    assert load_smiley_codes("s", tmp_path) == {CODE: "Hallo\n\ndu\n\n\nhaha"}


# --- how text reaches the app: typed like keyboard input, pasted only when really long ---


@pytest.mark.parametrize(
    "text",
    [
        MANY_BREAKS,
        "a\r\nb\rc\nd",
        "\n\n",
        "tab\t\tx",
        "\U0001f600\U0001f600",
        "Zeile1\n    eingerückt\n\tTab",
        "\n",
        "\U0001f600",
        "Hallo Welt",
        "x" * (NativeInjector.PASTE_FROM - 1),  # just below the limit
    ],
)
def test_text_is_typed_not_pasted(text):
    board = FakeClipboard()
    native = FakeNative(board)
    NativeInjector(native, board).inject_text(text)
    assert native.calls == [("text", text)]
    assert board.content == "alt"  # the clipboard was never touched


def test_only_really_long_text_is_pasted(fast):
    board = FakeClipboard()
    native = FakeNative(board)
    injector = NativeInjector(native, board)
    assert injector.pastes("x" * NativeInjector.PASTE_FROM) and not injector.pastes("x" * 100)
    injector.inject_text("a\r\nb\rc\nd" + "x" * NativeInjector.PASTE_FROM)
    assert native.calls == [("keys", "Ctrl+V", "a\r\nb\r\nc\r\nd" + "x" * NativeInjector.PASTE_FROM)]  # one CRLF each
    assert board.content == "alt"


@pytest.mark.parametrize("text", ["x" * 5000, MANY_BREAKS, "\U0001f600" * 3000])
def test_type_text_never_pastes(text):
    board = FakeClipboard()
    native = FakeNative(board)
    NativeInjector(native, board).type_text(text)
    assert native.calls == [("text", text)]
    assert board.content == "alt"


def test_text_is_typed_when_the_clipboard_is_busy():
    board = FakeClipboard(busy=True)
    native = FakeNative(board)
    NativeInjector(native, board).inject_text("x" * NativeInjector.PASTE_FROM)
    assert native.calls == [("text", "x" * NativeInjector.PASTE_FROM)]


# --- long text is typed in pieces ---


def test_long_text_is_typed_in_pieces_in_order(tmp_path, fake_engine, injector):
    text = "".join(f"{i:03d}\n" for i in range(120))  # 480 characters
    make_controller(tmp_path, fake_engine, injector, {CODE: text})
    fake_engine.letters(CODE)
    pieces = [value for kind, value in injector.calls if kind == "text"]
    assert len(pieces) == 5 and all(len(p) <= MorseController.TYPE_CHUNK for p in pieces)
    assert "".join(pieces) == text


def test_a_crlf_is_never_split_between_pieces(tmp_path, fake_engine, injector):
    text = "a" * (MorseController.TYPE_CHUNK - 1) + "\r\n" + "b" * 10
    make_controller(tmp_path, fake_engine, injector, {CODE: text})
    fake_engine.letters(CODE)
    pieces = [value for _, value in injector.calls]
    assert "".join(pieces) == text and not any(p.startswith("\n") for p in pieces)


def test_switching_off_stops_long_typing_after_the_current_piece(tmp_path, fake_engine, injector, listener):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "x" * 1000})
    controller.set_listener(listener)
    type_piece = injector.inject_text

    def type_then_switch_off(text):
        type_piece(text)
        fake_engine.active = False

    injector.inject_text = type_then_switch_off
    fake_engine.letters(CODE)
    assert len(injector.buffer) == MorseController.TYPE_CHUNK
    assert listener.errors == []


def test_text_that_gets_pasted_is_not_cut_in_pieces(tmp_path, fake_engine, injector):
    injector.pastes = lambda text: True
    make_controller(tmp_path, fake_engine, injector, {CODE: "x" * 1000})
    fake_engine.letters(CODE)
    assert injector.calls == [("text", "x" * 1000)]


@pytest.mark.parametrize("secret", ["x" * 350, "Pa\r\n" + "ss" * 200, "\U0001f600" * 250, "x" * 3000])
def test_clipboard_is_always_typed_even_when_long(tmp_path, fake_engine, injector, secret):
    injector.pastes = lambda text: True  # would be pasted if it were ordinary text
    injector.clipboard = secret
    make_controller(tmp_path, fake_engine, injector, {CODE: "{Clipboard}"})
    fake_engine.letters(CODE)
    assert "".join(value for _, value in injector.calls) == secret
    assert len(injector.calls) > 1  # typed in pieces

# --- {Clipboard} ---


def test_clipboard_is_typed_not_pasted_and_not_logged(tmp_path, fake_engine, injector):
    injector.clipboard = "pa$$w0rd {Enter}"  # braces from a password must not be read as commands
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "{Clipboard}{Enter}"})
    fake_engine.letters(CODE)
    assert injector.calls == [("text", "pa$$w0rd {Enter}"), ("keys", "Enter")]
    assert controller.typed_text == ""  # the live page must not show a password


@pytest.mark.parametrize("content", [None, ""])
def test_empty_clipboard_stops_with_a_message(tmp_path, fake_engine, injector, listener, content):
    injector.clipboard = content
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: "vorher{Clipboard}danach"})
    controller.set_listener(listener)
    fake_engine.letters(CODE)
    assert injector.calls == [("text", "vorher")]
    assert len(listener.errors) == 1 and "Zwischenablage" in listener.errors[0]


def test_injector_reads_the_clipboard_text():
    assert NativeInjector(FakeNative(), FakeClipboard(content="geheim")).clipboard_text() == "geheim"
    assert NativeInjector(FakeNative(), FakeClipboard(content=None)).clipboard_text() is None


def test_clipboard_text_needs_windows_and_reports_a_busy_clipboard():
    with pytest.raises(macros.MacroError, match="Windows"):
        NativeInjector(FakeNative(), FakeClipboard(on_windows=False)).clipboard_text()

    class Busy(FakeClipboard):
        def get_text(self):
            raise OSError("belegt")

    with pytest.raises(macros.MacroError, match="nicht lesbar"):
        NativeInjector(FakeNative(), Busy()).clipboard_text()


# --- {File …} ---


def test_file_macro_pastes_the_files(tmp_path, fake_engine, injector):
    doc, folder = tmp_path / "a b.txt", tmp_path / "Ordner"
    doc.write_text("x")
    folder.mkdir()
    make_controller(tmp_path, fake_engine, injector, {CODE: f"Anbei:{{Enter}}{{File {doc} | {folder}}}"})
    fake_engine.letters(CODE)
    assert injector.calls == [("text", "Anbei:"), ("keys", "Enter"), ("files", [str(doc), str(folder)])]


def test_missing_file_stops_before_anything_is_typed(tmp_path, fake_engine, injector, listener):
    controller = make_controller(tmp_path, fake_engine, injector, {CODE: f"Hallo{{File {tmp_path / 'weg.pdf'}}}"})
    controller.set_listener(listener)
    fake_engine.letters(CODE)
    assert injector.calls == []
    assert len(listener.errors) == 1 and "nicht gefunden" in listener.errors[0] and "Nichts getippt" in listener.errors[0]


def test_environment_variables_in_a_path_are_expanded(tmp_path, monkeypatch, fake_engine, injector):
    monkeypatch.setenv("MOURSE_TEST_DIR", str(tmp_path))
    variable = "%MOURSE_TEST_DIR%" if sys.platform == "win32" else "$MOURSE_TEST_DIR"
    make_controller(tmp_path, fake_engine, injector, {CODE: f"{{Ordner {variable}}}"})
    fake_engine.letters(CODE)
    assert injector.calls == [("files", [str(tmp_path)])]


def test_files_are_pasted_and_the_old_clipboard_comes_back(fast):
    board = FakeClipboard()
    native = FakeNative(board)
    NativeInjector(native, board).paste_files(["C:\\x"])
    assert native.calls == [("keys", "Ctrl+V", ("files", ["C:\\x"]))]  # the app saw the files at Ctrl+V
    assert board.content == "alt"


def test_files_are_not_pasted_over_a_clipboard_that_cannot_be_saved():
    board = FakeClipboard(busy=True)
    native = FakeNative(board)
    with pytest.raises(macros.MacroError, match="belegt"):
        NativeInjector(native, board).paste_files(["C:\\x"])
    assert native.calls == [] and board.content == "alt"


def test_files_need_windows():
    with pytest.raises(macros.MacroError, match="Windows"):
        NativeInjector(FakeNative(), FakeClipboard(on_windows=False)).paste_files(["x"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-Zwischenablage")
def test_real_clipboard_holds_files_like_explorer(tmp_path):
    first, second = tmp_path / "Datei eins.txt", tmp_path / "Ünïcode ✓"
    users = clipboard.save()
    try:
        clipboard.set_files([str(first), str(second)])
        formats = dict(clipboard.save())
        data = formats[clipboard.CF_HDROP]
        offset, _, _, _, wide = struct.unpack_from("<Iiiii", data)
        assert (offset, wide) == (20, 1)
        listing = data[offset:].decode("utf-16-le").rstrip("\0")
        assert listing.split("\0") == [str(first), str(second)]
        drop_effect = ctypes.windll.user32.RegisterClipboardFormatW("Preferred DropEffect")
        assert formats[drop_effect][:4] == struct.pack("<I", 1)  # copy, not move
    finally:
        clipboard.restore(users)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-Zwischenablage")
def test_real_clipboard_without_text_gives_none():
    users = clipboard.save()
    try:
        clipboard.set_files(["C:\\Windows"])
        assert clipboard.get_text() is None
        clipboard.set_text("Passwort")
        assert clipboard.get_text() == "Passwort"
    finally:
        clipboard.restore(users)
