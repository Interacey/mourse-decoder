"""The alphabet switch list with its shortcut, and the security hardening (network paths, import warning, formats)."""

from __future__ import annotations

import json

import pytest
from mourse_decoder import i18n, macros
from mourse_decoder._native import NATIVE_AVAILABLE
from mourse_decoder.controller import MorseController, NativeInjector
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.settings import MAX_CYCLE_ALPHABETS, Settings, SettingsStore
from test_macros import FakeClipboard, FakeNative, make_controller

# --- settings ---


def test_defaults_have_no_shortcut_and_an_empty_list():
    s = Settings()
    assert (s.cycle_shortcut, s.cycle_alphabets) == ("", ())
    assert s.validate() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"cycle_shortcut": "Ctrl+Foo"},
        {"cycle_shortcut": "Ctrl+Shift+Pause"},  # the on/off shortcut already uses it
        {"cycle_shortcut": "shift+ctrl+PAUSE"},  # same keys, written differently
        {"cycle_alphabets": ["latin", "german"]},  # a list instead of a tuple
        {"cycle_alphabets": ("Latin",)},
        {"cycle_alphabets": ("../etc",)},
        {"cycle_alphabets": ("",)},
        {"cycle_alphabets": (1, 2)},
        {"cycle_alphabets": ("latin", "latin")},
        {"cycle_alphabets": tuple(f"a{i}" for i in range(MAX_CYCLE_ALPHABETS + 1))},
        {"cycle_alphabets": None},
    ],
)
def test_bad_switch_settings_are_rejected(changes):
    assert Settings().replace(**changes).validate()


@pytest.mark.parametrize(
    "changes",
    [
        {"cycle_shortcut": "Ctrl+Alt+F9", "cycle_alphabets": ("latin", "german", "greek")},
        {"cycle_shortcut": "", "cycle_alphabets": ("latin",)},
        {"cycle_shortcut": "Ctrl+Alt+F9", "block_shortcut": ""},
        {"cycle_alphabets": tuple(f"a{i}" for i in range(MAX_CYCLE_ALPHABETS))},
        {"cycle_alphabets": ("chinese-telecode", "wabun", "mein_alphabet-2")},
    ],
)
def test_good_switch_settings_are_accepted(changes):
    assert Settings().replace(**changes).validate() == []


def test_switch_list_survives_saving_and_loading(tmp_path):
    store = SettingsStore(tmp_path)
    settings = Settings().replace(cycle_shortcut="Ctrl+Alt+F9", cycle_alphabets=("greek", "latin"))
    store.save(settings)
    assert json.loads(store.path.read_text(encoding="utf-8"))["cycle_alphabets"] == ["greek", "latin"]  # a JSON list
    loaded = store.load()
    assert loaded == settings and loaded.cycle_alphabets == ("greek", "latin")  # the order is the user's


def test_a_settings_file_from_before_the_list_still_loads(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text(json.dumps({"alphabet": "german", "dot_threshold_ms": 250}), encoding="utf-8")
    loaded = store.load()
    assert (loaded.alphabet, loaded.cycle_alphabets, loaded.cycle_shortcut) == ("german", (), "")


def test_a_broken_list_in_the_file_falls_back_to_defaults(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text(json.dumps({"cycle_alphabets": "latin"}), encoding="utf-8")
    assert store.load() == Settings()
    assert store.path.with_suffix(".json.bak").exists()  # kept for the user, like any broken file


# --- which alphabet comes next ---

ALL = {"latin", "german", "greek", "cyrillic"}


@pytest.mark.parametrize(
    ("alphabet", "items", "steps", "expected"),
    [
        ("latin", ("latin", "german", "greek"), 1, ("german", 2, 3)),
        ("greek", ("latin", "german", "greek"), 1, ("latin", 1, 3)),  # wraps around
        ("latin", ("latin", "german", "greek"), 2, ("greek", 3, 3)),  # two presses in one poll
        ("latin", ("latin", "german", "greek"), 4, ("german", 2, 3)),
        ("cyrillic", ("latin", "german"), 1, ("latin", 1, 2)),  # not in the list: the first press goes to its start
        ("latin", ("latin",), 1, ("latin", 1, 1)),  # a list of one stays put
        ("latin", ("latin", "gone", "greek"), 1, ("greek", 2, 2)),  # deleted alphabets are skipped
        ("latin", ("greek", "latin", "german"), 1, ("german", 3, 3)),  # the user's order, not alphabetical
    ],
)
def test_next_alphabet(alphabet, items, steps, expected):
    settings = Settings().replace(alphabet=alphabet, cycle_alphabets=items)
    assert settings.cycle_target(ALL, steps) == expected


@pytest.mark.parametrize("items", [(), ("gone",), ("gone", "also-gone")])
def test_no_usable_entry_gives_none(items):
    assert Settings().replace(cycle_alphabets=items).cycle_target(ALL) is None


# --- controller ---


def test_the_engine_gets_the_cycle_shortcut(fake_engine, injector):
    settings = Settings().replace(cycle_shortcut="Ctrl+Alt+F9")
    controller = MorseController(fake_engine, injector, DictionaryManager(), settings, use_hook=False)
    controller.start()
    assert fake_engine.cycle_shortcut == "Ctrl+Alt+F9"
    controller.apply_settings(settings.replace(cycle_shortcut=""))
    assert fake_engine.cycle_shortcut == ""


def test_presses_are_counted_once(fake_engine, injector):
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings(), use_hook=False)
    fake_engine.cycle_count = 7  # pressed before the app started: not ours
    controller.start()
    assert controller.poll_cycle() == 0
    fake_engine.cycle_count = 9
    assert controller.poll_cycle() == 2
    assert controller.poll_cycle() == 0


def test_the_counter_wraps_around(fake_engine, injector):
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings(), use_hook=False)
    fake_engine.cycle_count = 2**32 - 1
    controller.start()
    fake_engine.cycle_count = 1
    assert controller.poll_cycle() == 2


def test_nothing_is_counted_while_stopped(fake_engine, injector):
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings(), use_hook=False)
    fake_engine.cycle_count = 5
    assert controller.poll_cycle() == 0


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="Rust-Modul nicht gebaut")
def test_the_real_engine_takes_the_shortcut_and_counts():
    from mourse_decoder import _morse_core as core

    engine = core.MorseEngine()
    before = engine.cycle_count
    engine.cycle_shortcut = "Ctrl+Alt+F9"
    engine.cycle_shortcut = ""
    assert isinstance(before, int) and engine.cycle_count == before  # nothing pressed, nothing counted
    with pytest.raises(ValueError):
        engine.cycle_shortcut = "Ctrl+Foo"


# --- no network paths ---


@pytest.mark.parametrize(
    "path",
    ["\\\\server\\share\\x.pdf", "//server/share/x.pdf", "\\\\10.0.0.5\\c$\\x", "\\\\?\\UNC\\server\\share\\x", "\\\\.\\pipe\\x"],
)
def test_network_paths_are_refused(path):
    with pytest.raises(macros.MacroError, match="Netzwerkpfade"):
        macros.resolve_path(path)


def test_a_network_path_from_an_environment_variable_is_refused(monkeypatch):
    monkeypatch.setenv("MOURSE_SHARE", "\\\\evil.example\\share")
    with pytest.raises(macros.MacroError, match="Netzwerkpfade"):
        macros.resolve_path("%MOURSE_SHARE%\\x.pdf")
        macros.resolve_path("$MOURSE_SHARE/x.pdf")  # the same on systems without %VAR%


@pytest.mark.parametrize("path", ["C:\\Windows", "relativ\\datei.txt", "~", "D:/Daten/x y.txt"])
def test_local_paths_are_fine(path):
    assert not macros.resolve_path(path).startswith(("\\\\", "//"))


def test_a_macro_with_a_network_path_types_nothing_and_never_looks_at_the_share(
    tmp_path, fake_engine, injector, listener, monkeypatch
):
    import mourse_decoder.controller as controller_module

    looked = []
    monkeypatch.setattr(controller_module.os.path, "exists", lambda p: looked.append(p) or True)
    controller = make_controller(tmp_path, fake_engine, injector, {"--..--..": "Hallo{File \\\\evil.example\\share\\x.pdf}"})
    controller.set_listener(listener)
    fake_engine.letters("--..--..")
    assert injector.calls == [] and looked == []  # not even the existence check, which is what leaks the login
    assert len(listener.errors) == 1 and "Netzwerkpfade" in listener.errors[0]


def test_the_injector_refuses_a_network_path_itself():
    board = FakeClipboard()
    native = FakeNative(board)
    with pytest.raises(macros.MacroError, match="Netzwerkpfade"):
        NativeInjector(native, board).paste_files(["\\\\evil.example\\share\\x.pdf"])
    assert native.calls == [] and board.content == "alt"


# --- warning before importing commands ---


def write_set(tmp_path, codes, name="x.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"id": "x", "codes": codes}), encoding="utf-8")
    return path


def test_commands_that_do_something_are_listed(tmp_path):
    path = write_set(
        tmp_path,
        {
            "--..--..": "{Win+R}{WaitWindow Run}cmd{Enter}",
            "--..-.-.": "Passwort: {Clipboard}",
            "--...-..": "{File C:\\a.txt} und {Datei D:\\b}",
            "-.-..--.": "{Ctrl+C}{ctrl+c}",
        },
    )
    assert macros.commands_in_file(path) == ["{Win+R}", "{Enter}", "{Clipboard}", "{File …}", "{Ctrl+C}", "{ctrl+c}"]


@pytest.mark.parametrize(
    "codes",
    [
        {"--..--..": "Hallo Welt"},
        {"--..--..": "{Wait 500}{Loop 3}x{EndLoop}{WaitWindow Editor}"},  # waiting and loops do nothing by themselves
        {"--..--..": "{^_^} { } {}"},  # braces that are only text
        {"--..--..": "😀\n\n\ttext"},
    ],
)
def test_harmless_files_give_no_warning(tmp_path, codes):
    assert macros.commands_in_file(write_set(tmp_path, codes)) == []


def test_commands_inside_loops_are_found(tmp_path):
    assert macros.commands_in_file(write_set(tmp_path, {"--..--..": "{Loop 2}{Win+R}{EndLoop}"})) == ["{Win+R}"]


@pytest.mark.parametrize("content", ["{ kaputt", "[]", "{}", '{"codes": []}', '{"codes": {"-.": 5}}', "\x00\xff", ""])
def test_unreadable_files_give_no_warning_and_no_crash(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_bytes(content.encode("latin-1"))
    assert macros.commands_in_file(path) == []


def test_a_missing_file_gives_no_warning(tmp_path):
    assert macros.commands_in_file(tmp_path / "gibt-es-nicht.json") == []


def test_broken_macros_in_a_file_are_skipped_not_listed(tmp_path):
    path = write_set(tmp_path, {"--..--..": "{Loop 2}{Win+R}", "--..-.-.": "{Ctrl+C}"})  # the first has no {EndLoop}
    assert macros.commands_in_file(path) == ["{Ctrl+C}"]


def test_the_alphabet_files_that_ship_with_the_app_need_no_warning():
    from mourse_decoder.dictionary import BUILTIN_ALPHABET_DIR
    from mourse_decoder.smileys import BUILTIN_SMILEY_DIR

    for folder in (BUILTIN_ALPHABET_DIR, BUILTIN_SMILEY_DIR):
        for path in folder.glob("*.json"):
            assert macros.commands_in_file(path) == [], path.name


# --- language files cannot read objects ---


@pytest.fixture(autouse=True)
def english():
    yield
    i18n.set_language("en")


def language_with(tmp_path, text):
    (tmp_path / "evil.json").write_text(
        json.dumps({"id": "evil", "name": "Evil", "strings": {"update.error": text}}), encoding="utf-8"
    )
    i18n.set_language("evil", tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "{msg.__class__.__init__.__globals__}",
        "{msg.args}",
        "{msg[0]}",
        "{0}",
        "{msg!r:>{msg}}",
        "{}",
        "{msg.__traceback__}",
    ],
)
def test_a_placeholder_cannot_reach_into_an_object(tmp_path, text):
    language_with(tmp_path, text)
    secret = ValueError("geheim")
    shown = i18n.tr("update.error", msg=secret)
    assert "geheim" not in shown and "globals" not in shown.lower().replace("{msg.__class__.__init__.__globals__}", "")
    assert shown == text  # the raw text, like for any other bad placeholder


def test_normal_placeholders_still_work(tmp_path):
    language_with(tmp_path, "Fehler: {msg} ({n}/{total})")
    assert i18n.tr("update.error", msg="kaputt", n=2, total=5) == "Fehler: kaputt (2/5)"
    assert i18n.tr("update.error", msg="x") == "Fehler: {msg} ({n}/{total})"  # a missing value keeps the text


def test_every_built_in_text_still_formats():
    # the plain formatter must accept every placeholder the shipped languages use
    for code in ("en", "de", "de-saar", "es", "fr", "it", "pt"):
        i18n.set_language(code)
        for key in i18n._strings:
            text = i18n._strings[key]
            fields = {name for _, name, _, _ in __import__("string").Formatter().parse(text) if name}
            values = {name: "x" for name in fields}
            assert "{" not in i18n.tr(key, **values) or not fields, (code, key)
