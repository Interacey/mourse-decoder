"""Fourth review round: key parsing parity with Rust, one check for single files and bundles, trusted dialog texts,
nesting depth, file names, telecode values, lone surrogates in the user's files, message lengths."""

from __future__ import annotations

import inspect
import json
import os
import queue
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from mourse_decoder import _native, alphabet_files, app, backup, backup_content, i18n, jsontext, macros
from mourse_decoder import settings as settings_mod
from mourse_decoder.dictionary import AlphabetError, DictionaryManager
from mourse_decoder.i18n import set_language, tr, tr_trusted
from mourse_decoder.settings import Settings, SettingsStore
from test_backup import make_config
from test_import_security import alphabet_bytes, make_bundle
from test_round3_reviews import junction, make_scratch

needs_native = pytest.mark.skipif(not _native.NATIVE_AVAILABLE, reason="Rust module not built")


def rust_binding(spec: str):
    """What the Rust parser makes of ``spec``: its printed form, or None if it refuses."""
    try:
        return _native.load_native().MorseEngine(trigger=spec).trigger
    except ValueError:
        return None


# ---- SE1: Python accepts no key that Rust refuses ------------------------------------------------------------------

REVIEW_SPECS = [
    "F8\x1f", "\x1fF8", "Ctrl\x1c+F8", "Ctrl\x1f+F9", "Ctrl+\x1dF8", "V\u212a81", "CapsLoc\u212a+F8", "Mou\u017fe5",
    "Ctrl+Shift+Pau\u017fe", "F8\u2028\x1f", "\uff26\uff18",
]


@needs_native
@pytest.mark.parametrize("spec", REVIEW_SPECS, ids=ascii)
def test_the_specs_that_only_python_accepted_are_refused_everywhere(spec):
    assert rust_binding(spec) is None
    assert not settings_mod.is_valid_binding(spec)
    for field in ("trigger", "block_shortcut", "cycle_shortcut", "undo_shortcut"):
        assert Settings(**{field: spec}).validate(), field
    assert Settings(separate_dash_key=True, dash_trigger=spec).validate()


@needs_native
def test_python_valid_implies_rust_valid_and_prints_the_same_form_over_a_fuzz():
    rng = random.Random(20260213)
    blanks = [chr(c) for c in range(0x20)] + [
        "\x7f", "\x85", "\xa0", "\u1680", "\u2000", "\u200a", "\u2028", "\u2029", "\u202f", "\u205f", "\u3000",
        "\u200b", "\ufeff",
    ]
    tricky = ["\u212a", "\u017f", "\u0131", "\u0130", "\uff0b", "\uff26", "\uff18", "\u0661", "\u00df", "\u1e9e"]
    mods = ["Ctrl", "ctrl", "STRG", "Alt", "Shift", "Umschalt", "Win", "Meta", "Super", "CapsLock", "Caps", "Control"]
    keys = [*settings_mod.TRIGGER_NAMES, "F13", "F24", "f8", "scrolllock", "VK81", "vk255", "VK0", "VK256", "Mouse4",
            "mouse5", "Mouse3", "Mouse33", "F25", "F012"]

    def mutate(text: str) -> str:
        i = rng.randrange(len(text) + 1)
        pool = blanks if rng.random() < 0.5 else tricky
        return text[:i] + rng.choice(pool) + (text[i:] if rng.random() < 0.7 else text[i + 1:])

    started = time.monotonic()
    valid = 0
    for _ in range(6000):
        spec = "+".join([*(rng.choice(mods) for _ in range(rng.randrange(3))), rng.choice(keys)])
        for _ in range(rng.randrange(3)):
            spec = mutate(spec)
        if settings_mod.is_valid_binding(spec):
            valid += 1
            assert rust_binding(spec) == settings_mod.normalize_binding(spec), ascii(spec)
        assert time.monotonic() - started < 60
    assert valid > 500  # the fuzz reaches the valid side too


OLD_VALUES = [
    "F8", "ScrollLock", "Pause", "MouseMiddle", "Ctrl+Shift+Pause", "CapsLock+VK81", "Ctrl+Alt+F9", "MouseLeft",
    "Space", "F13", "VK81", "Mouse5", "Alt+Shift+Insert", "ControlRight", "AltGr",
]


@pytest.mark.parametrize("value", OLD_VALUES)
def test_triggers_users_have_saved_today_stay_valid_and_the_file_stays_as_it_is(tmp_path, value):
    assert settings_mod.is_valid_binding(value)
    if _native.NATIVE_AVAILABLE:
        assert rust_binding(value) is not None
    text = json.dumps({"trigger": value, "dash_trigger": "F9", "block_shortcut": "Ctrl+Shift+Pause"}, indent=2)
    (tmp_path / "settings.json").write_text(text, encoding="utf-8")
    store = SettingsStore(tmp_path)
    loaded = store.load()
    assert loaded.trigger == settings_mod.normalize_binding(value) and store.warnings == []
    assert (tmp_path / "settings.json").read_text(encoding="utf-8") == text  # loading writes nothing


def test_keys_are_loaded_in_the_spelling_the_hook_prints(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"trigger": "VK124", "block_shortcut": "strg+shift+Pause"}), encoding="utf-8"
    )
    loaded = SettingsStore(tmp_path).load()
    assert (loaded.trigger, loaded.block_shortcut) == ("F13", "Ctrl+Shift+Pause")


@needs_native
def test_a_difference_between_the_parsers_is_caught_by_the_engine_itself(monkeypatch):
    monkeypatch.setattr(settings_mod, "is_valid_binding", lambda spec: True)  # a Python parser that is too lax
    monkeypatch.setattr(settings_mod, "is_valid_shortcut", lambda spec: True)
    settings_mod.native_binding_error.cache_clear()
    for field in ("trigger", "block_shortcut", "cycle_shortcut", "undo_shortcut"):
        errors = Settings(**{field: "F8\x1f"}).validate()
        assert any("Tastatur-Bibliothek" in e for e in errors), (field, errors)
    assert Settings(trigger="F8").validate() == []


@needs_native
@pytest.mark.parametrize("field", ["trigger", "block_shortcut", "cycle_shortcut", "undo_shortcut", "dash_trigger"])
def test_a_backup_with_such_a_key_is_refused_and_a_start_with_the_file_does_not_crash(tmp_path, field):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    changes = {field: "V\u212a81", "separate_dash_key": field == "dash_trigger"}
    data = json.dumps(changes).encode()
    with pytest.raises(backup.BackupError) as info:
        backup.inspect_bundle(make_bundle(tmp_path / "b.zip", {"settings.json": data}), config)
    assert info.value.code == "file_unusable"
    folder = tmp_path / "own"
    folder.mkdir()
    (folder / "settings.json").write_bytes(data)  # a file that is on the disk anyway
    store = SettingsStore(folder)
    assert store.load() == Settings()  # the start goes on with the defaults
    assert (folder / "settings.json.bak").is_file()  # and the file is kept
    assert [key for key, _ in store.warnings] == ["startup.settings_discarded"]
    engine, settings, warnings = app.make_engine(_native.load_native(), store.load(), store)
    assert engine.trigger == Settings().trigger and warnings == []


def test_the_engine_is_made_with_default_keys_when_it_refuses_the_ones_of_the_file(tmp_path):
    set_language("en")
    (tmp_path / "settings.json").write_text('{"trigger": "F9"}', encoding="utf-8")
    store = SettingsStore(tmp_path)
    calls = []

    class Native:
        class MorseEngine:
            def __init__(self, trigger):
                calls.append(trigger)
                if trigger == "F9":
                    raise ValueError("Unbekannter Taster 'F9'")
                self.trigger = trigger

    engine, settings, warnings = app.make_engine(Native, store.load(), store)
    assert calls == ["F9", Settings().trigger] and engine.trigger == Settings().trigger
    assert settings.trigger == Settings().trigger and settings.block_shortcut == "" and len(warnings) == 1
    assert (tmp_path / "settings.json.bak").is_file() and (tmp_path / "settings.json").read_text() == '{"trigger": "F9"}'
    calls.clear()
    Native.MorseEngine.__init__ = lambda self, trigger: setattr(self, "trigger", trigger)
    assert app.make_engine(Native, Settings(), store)[2] == []


# ---- SE2: one check for a single file and a bundle -------------------------------------------------------------------

WORD_SEPARATOR_ONLY = json.dumps({"id": "klingon", "name": "K", "extends": "common", "word_separator": "{Win+R}cmd{Enter}"})


@pytest.mark.parametrize(
    "document",
    [
        WORD_SEPARATOR_ONLY,
        json.dumps({"id": "k", "extends": "common", "codes": {}, "word_separator": "{Win+R}"}),
        json.dumps({"id": "k", "extends": "common", "codes": None, "word_separator": "\x1b"}),
    ],
    ids=["no-codes", "empty-codes", "codes-null"],
)
def test_a_file_without_codes_but_with_a_word_separator_is_judged_in_both_paths(tmp_path, document, monkeypatch):
    from tkinter import messagebox

    from mourse_decoder.ui import theme

    path = tmp_path / "k.json"
    path.write_text(document, encoding="utf-8")
    assert macros.commands_in_file(path)  # the single-file import
    assert backup_content.commands_per_code(document.encode())  # a bundle
    asked = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **kw: asked.append(a) or False)
    assert theme.confirm_commands(None, path) is False and asked
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    preview = backup.inspect_bundle(make_bundle(tmp_path / "b.zip", {"alphabets/k.json": document.encode()}), config)
    assert preview.risky_files == ("alphabets/k.json",)
    assert [c[0] for c in macros.typed_texts(json.loads(document))][-1] == "word_separator"


def test_both_paths_share_one_function_and_agree_on_every_shipped_file():
    sources = inspect.getsource(macros.commands_in_file) + inspect.getsource(backup_content._typed_texts)
    assert sources.count("typed_texts") >= 2
    for path in Path(backup_content.__file__).parent.glob("*/*.json"):
        if path.parent.name in ("alphabets", "smiley_sets"):
            assert macros.commands_in_file(path) == [] == backup_content.commands_per_code(path.read_bytes())


def test_a_user_file_that_extends_another_is_judged_by_its_own_texts_and_the_base_by_its_own(tmp_path):
    base = alphabet_bytes({".-": "{Win+R}"})
    child = json.dumps({"id": "child", "extends": "evil", "word_separator": " "}).encode()
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    preview = backup.inspect_bundle(
        make_bundle(tmp_path / "b.zip", {"alphabets/evil.json": base, "alphabets/child.json": child}), config
    )
    assert preview.risky_files == ("alphabets/evil.json",)  # the child adds no text of its own; the base is checked


# ---- SE3: texts of decisions come from the built-in languages ---------------------------------------------------------

SWAPPED = {
    "id": "xx", "name": "Xx",
    "strings": {"backup.btn.yes": "No", "backup.btn.no": "Yes", "backup.btn.cancel": "Yes", "import.risky.title": "Hello",
                "import.risky": "All fine, {file}", "backup.import": "Harmless", "backup.err.sqlite_values": "Fine"},
}


def two_stage_attack(tmp_path):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    (config / "settings.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")
    bundle = make_bundle(
        tmp_path / "b.zip",
        {"languages/xx.json": json.dumps(SWAPPED).encode(), "settings.json": json.dumps({"language": "xx"}).encode()},
    )
    return config, bundle


def test_a_backup_that_brings_and_selects_a_language_is_named_in_the_confirmation(tmp_path):
    config, bundle = two_stage_attack(tmp_path)
    preview = backup.inspect_bundle(bundle, config)
    note = next(n for n in preview.notes if n.code == "languages_custom")
    assert note.params["names"] == "xx"
    from mourse_decoder.ui.settings_panel import SettingsPanel

    assert "built-in texts" in SettingsPanel._confirm_text(None, preview)


def test_no_language_note_for_the_own_backup_or_a_built_in_language(tmp_path):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    (config / "languages" / "xx.json").write_text(json.dumps(SWAPPED), encoding="utf-8")
    (config / "settings.json").write_text(json.dumps({"language": "xx"}), encoding="utf-8")
    own = tmp_path / "own.zip"
    backup.export_bundle(config, own)
    assert not [n for n in backup.inspect_bundle(own, config).notes if n.code == "languages_custom"]
    german = make_bundle(tmp_path / "de.zip", {"settings.json": json.dumps({"language": "de"}).encode()})
    assert not [n for n in backup.inspect_bundle(german, config).notes if n.code == "languages_custom"]


def test_after_the_two_stage_attack_every_decision_text_is_still_the_built_in_one(tmp_path, monkeypatch):
    from tkinter import messagebox

    from mourse_decoder.ui import settings_panel as sp
    from mourse_decoder.ui import theme

    config, bundle = two_stage_attack(tmp_path)
    backup.import_bundle(bundle, config)
    set_language("xx", config / "languages")
    try:
        assert tr("backup.btn.yes") == "No"  # the user's language file does what it says ...
        assert tr_trusted("backup.btn.yes") == "Yes" and sp._trusted("backup.btn.no") == "No"  # ... the dialogs ignore it
        asked = []
        monkeypatch.setattr(messagebox, "askyesno", lambda title, message, **kw: asked.append((title, message)) or False)
        path = tmp_path / "k.json"
        path.write_text(WORD_SEPARATOR_ONLY, encoding="utf-8")
        theme.confirm_commands(None, path)
        title, message = asked[0]
        assert title == tr_trusted("import.risky.title") != "Hello" and "All fine" not in message
        error = backup.BackupError("sqlite_values", name="telecode.sqlite", count=3)
        assert sp.SettingsPanel._error_text(None, error, trusted=True) == tr_trusted("backup.err.sqlite_values", **error.params)
        assert sp.SettingsPanel._error_text(None, error) == "Fine"  # an ordinary error text follows the language
    finally:
        set_language("en")


def test_the_buttons_of_the_question_come_from_the_built_in_texts(tk_root, tmp_path, monkeypatch):
    import tkinter as tk

    from mourse_decoder.ui import settings_panel as sp
    from mourse_decoder.ui import theme

    theme.apply_theme(tk_root, "light")
    config, bundle = two_stage_attack(tmp_path)
    backup.import_bundle(bundle, config)
    set_language("xx", config / "languages")
    shown = []
    real_button = tk.Button
    monkeypatch.setattr(sp.tk, "Button", lambda *a, **kw: shown.append(kw.get("text")) or real_button(*a, **kw))
    for name in ("wait_window", "wait_visibility", "grab_set"):
        monkeypatch.setattr(tk.Toplevel, name, lambda self, *a: None)
    try:
        sp.BackupDialogs(tk_root).choose("t", "question")
    finally:
        set_language("en")
    assert shown == [tr_trusted("backup.btn.yes"), tr_trusted("backup.btn.no"), tr_trusted("backup.btn.cancel")]
    assert shown[0] != "No"


# ---- SE4: one nesting limit everywhere ------------------------------------------------------------------------------


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


@pytest.mark.parametrize("depth", [101, 500, 16896, 16900, 60000])
def test_deep_nesting_is_refused_by_json_loading_not_by_the_interpreter(depth):
    with pytest.raises(json.JSONDecodeError):
        jsontext.loads(f'{{"a": {nested(depth)}}}', dict)


def test_the_limit_is_fixed_and_strings_do_not_count():
    assert jsontext.loads(f'{{"a": {nested(jsontext.MAX_JSON_DEPTH - 1)}}}', dict)
    macro = "{" * 500 + "[" * 500 + '\\"' + "{"
    assert jsontext.loads(json.dumps({"codes": {".-": macro}}), dict)["codes"][".-"] == macro


@pytest.mark.parametrize("depth", [101, 16896, 16900])
def test_every_loader_treats_a_deep_file_as_a_broken_one(tmp_path, depth):
    deep = f'{{"id": "deep", "name": {nested(depth)}, "codes": {{".-": "a"}}}}'
    user = tmp_path / "alphabets"
    user.mkdir()
    (user / "deep.json").write_text(deep, encoding="utf-8")
    manager = DictionaryManager(user_directory=user)
    assert "deep" not in [a.id for a in manager.available()]  # listing must not raise
    with pytest.raises(AlphabetError):
        manager.load("deep")
    language = tmp_path / "xx.json"
    language.write_text(f'{{"id": "xx", "strings": {nested(depth)}}}', encoding="utf-8")
    assert i18n._read(language) is None
    (tmp_path / "settings.json").write_text(f'{{"alphabet": {nested(depth)}}}', encoding="utf-8")
    store = SettingsStore(tmp_path)
    assert store.load() == Settings() and [k for k, _ in store.warnings] == ["startup.settings_discarded"]
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    with pytest.raises(backup.BackupError):  # like any file that is no JSON: the bundle is refused, not half read
        backup.inspect_bundle(make_bundle(tmp_path / "b.zip", {"alphabets/deep.json": deep.encode()}), config)
    with pytest.raises(backup.BackupError):
        backup.inspect_bundle(
            make_bundle(tmp_path / "c.zip", {"function_keys.json": f'{{"schema_version": 1, "slots": {nested(depth)}}}'.encode()}),
            config,
        )


# ---- SE5: names ---------------------------------------------------------------------------------------------------


def test_command_paths_are_written_out_for_the_dialog_but_stay_exact_for_the_import():
    summary = backup.CommandSummary("alphabets/a\u2062b.json", ".-", "{Win+R}", ("{Win+R}",))
    assert summary.path == "alphabets/a\u2062b.json" and summary.label == "alphabets/a\\u2062b.json"


@pytest.mark.parametrize(
    "name", ["a\u2062b", "x\u2800", "y\u206a", "z\U000e0041", "w\u202e", "v\u00ad", "u\u3164", "t\u2028", "s\x85", "h\u115f",
             "f\uffa0", "k\u2061", "m\u180e"],
    ids=ascii,
)
def test_names_with_invisible_characters_are_refused(name):
    assert not backup.is_allowed_name(f"alphabets/{name}.json")


@pytest.mark.parametrize("name", ["é", "mein-alphabet", "ä_ö", "中文", "a\u200db", "a\u200cb", "emoji-😀", "Größe"])
def test_legitimate_names_are_not_refused(name):
    assert backup.is_allowed_name(f"alphabets/{name}.json")


# ---- SE7: telecode values ----------------------------------------------------------------------------------------------


def telecode_with(path: Path, rows) -> bytes:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE telecode(code TEXT PRIMARY KEY, char) WITHOUT ROWID")
    conn.executemany("INSERT INTO telecode VALUES(?, ?)", rows)
    conn.commit()
    conn.close()
    return path.read_bytes()


@pytest.mark.parametrize("value", [b"\xff\xfe", 7, 1.5])
def test_a_telecode_value_that_is_not_text_is_refused_in_an_import(tmp_path, value):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    data = telecode_with(tmp_path / "t.sqlite", [("0001", "中"), ("0002", value)])
    with pytest.raises(backup.BackupError) as info:
        backup.inspect_bundle(make_bundle(tmp_path / "b.zip", {"telecode.sqlite": data}), config)
    assert info.value.code == "sqlite_values" and "1 entries" in str(info.value)


def test_a_telecode_row_that_is_not_text_types_nothing_and_stops_nothing(tmp_path):
    from mourse_decoder.decoders import TelecodeDecoder
    from mourse_decoder.telecode import TelecodeStore

    path = tmp_path / "t.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE telecode(code TEXT PRIMARY KEY, char) WITHOUT ROWID")
    conn.execute("INSERT INTO telecode VALUES('0001', x'fffe')")
    conn.execute("INSERT INTO telecode VALUES('0002', 5)")
    conn.execute("INSERT INTO telecode VALUES('0003', CAST(x'edb080' AS TEXT))")  # not valid UTF-8
    conn.commit()
    conn.close()
    store = TelecodeStore(path)
    assert [store.lookup(c) for c in ("0001", "0002", "0003")] == [None, None, None]
    codes = DictionaryManager().load("common").codes
    seq = {digit: code for code, digit in codes.items() if digit.isdigit()}
    decoder = TelecodeDecoder(store, codes)
    emission = None
    for digit in "0001":
        emission = decoder.decode(seq[digit])
    assert emission is not None and emission.text == "1" and emission.erase == 0  # the digit stays, like an unknown code
    store.close()


# ---- CM1: lone surrogates in the user's own files ------------------------------------------------------------------


SPLIT = '{"id":"split","name":"Split","extends":"latin","codes":{".-.-.-":"x\\ud83d"}}'


def hand_made(tmp_path):
    cfg = make_config(tmp_path / "MourseDecoder")
    (cfg / "alphabets" / "split.json").write_text(SPLIT, encoding="utf-8")  # an emoji cut in two by an editor
    return cfg


def test_the_own_export_of_a_folder_with_such_a_file_can_be_imported_again(tmp_path):
    cfg = hand_made(tmp_path)
    export = tmp_path / "export.zip"
    backup.export_bundle(cfg, export)
    preview = backup.inspect_bundle(export, cfg)
    assert preview.count("unchanged") >= 1
    backup.import_bundle(export, cfg)  # nothing to do, and it must not fail
    assert (cfg / "alphabets" / "split.json").read_text(encoding="utf-8") == SPLIT


def test_foreign_import_and_undo_with_such_a_file_in_the_folder(tmp_path):
    cfg = hand_made(tmp_path)
    other = make_config(tmp_path / "other")
    (other / "alphabets" / "neu.json").write_text(
        json.dumps({"id": "neu", "name": "Neu", "codes": {".": "e"}}), encoding="utf-8"
    )
    backup.export_bundle(other, tmp_path / "in.zip")
    backup.import_bundle(tmp_path / "in.zip", cfg)
    (restore_point,) = tmp_path.glob("MourseDecoder-backup-*.zip")
    undo = backup.inspect_bundle(restore_point, cfg)  # used to be refused: file_unusable
    assert "remove" in [e.status for e in undo.entries]
    backup.import_bundle(restore_point, cfg)
    assert not (cfg / "alphabets" / "neu.json").exists() and (cfg / "alphabets" / "split.json").is_file()


def test_such_a_file_does_not_replace_a_good_one_and_is_only_a_note_otherwise(tmp_path):
    cfg = make_config(tmp_path / "cfg" / "MourseDecoder")
    new = make_bundle(tmp_path / "n.zip", {"alphabets/split.json": SPLIT.encode()})
    assert [n.code for n in backup.inspect_bundle(new, cfg).notes] == ["file_skipped"]
    good = make_bundle(tmp_path / "g.zip", {"alphabets/mein.json": SPLIT.replace("split", "mein").encode()})
    with pytest.raises(backup.BackupError) as info:  # make_config has a good mein.json: a broken one must not replace it
        backup.inspect_bundle(good, cfg)
    assert info.value.code == "file_unusable"


def test_the_alphabet_loaders_and_the_editor_refuse_a_lone_surrogate_cleanly(tmp_path):
    user = tmp_path / "alphabets"
    user.mkdir()
    manager = DictionaryManager(user_directory=user)
    text = '{"id": "split", "name": "S", "extends": "latin", "codes": {".-.-.-": "x\ud83d"}}'  # a real lone surrogate
    with pytest.raises(AlphabetError):
        alphabet_files.save_alphabet_text(manager, text)
    assert list(user.iterdir()) == []  # no file and no temp file left behind


# ---- CM2/CM3: message lengths ---------------------------------------------------------------------------------------


def test_the_list_of_files_of_a_failed_write_is_complete():
    names = ", ".join(f"alphabets/a-long-name-{i:02d}.json" for i in range(20))
    error = backup.BackupError("write_failed_partial", name="x.json", error="disk full", files=names)
    assert error.params["files"] == names and all(f"{i:02d}" in str(error) for i in range(20))
    assert len(backup.BackupError("file_unusable", name="x", reason="r" * 5000, path="x").params["reason"]) <= 301


def test_the_note_about_keys_lists_every_field_by_the_name_the_settings_page_gives_it(tmp_path):
    from mourse_decoder.ui import settings_panel as sp

    set_language("en")
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    SettingsStore(config).save(Settings(alphabet="latin", trigger="ScrollLock"))
    other = Settings(
        alphabet="latin", trigger="F8", separate_dash_key=True, dash_trigger="F9", block_input=True,
        block_shortcut="Ctrl+Shift+F7", cycle_shortcut="Ctrl+Alt+F5", undo_shortcut="Ctrl+Alt+F6",
    )
    SettingsStore(tmp_path / "o").save(other)
    bundle = make_bundle(tmp_path / "b.zip", {"settings.json": (tmp_path / "o" / "settings.json").read_bytes()})
    (note,) = [n for n in backup.inspect_bundle(bundle, config).notes if n.code == "settings_keys"]
    assert note.params["fields"].split(", ") == list(backup.KEY_FIELDS)  # all seven, none cut
    text = sp._note_text(note)
    assert "undo_shortcut" not in text and tr_trusted("settings.undo_shortcut") in text and tr_trusted("settings.key") in text


def test_a_loader_reason_in_a_note_keeps_its_length():
    note = backup_content.Note("file_skipped", {"name": "x" * 300, "reason": "r" * 400})
    assert len(note.params["reason"]) == 301 and len(note.params["name"]) == 81


def test_the_restore_point_is_named_once_even_when_its_path_is_long_or_has_hidden_characters():
    from mourse_decoder.ui.settings_panel import SettingsPanel

    point = Path("C:/Users/x/" + "d" * 400 + "\u202e/MourseDecoder-backup-20260101-000000.zip")
    error = backup.BackupError("write_failed_partial_point", name="x", error="e", files="a.json", restore_point=point)
    text = SettingsPanel._error_text(None, error)
    assert "\n\n" not in text and "\u202e" not in text  # the path is in the message once, nothing is appended
    plain = backup.BackupError("write_failed_partial_point", name="x", error="e", files="a", restore_point=Path("C:/r.zip"))
    assert SettingsPanel._error_text(None, plain).count("r.zip") == 1


def test_the_warning_text_for_telecode_says_what_really_happens_in_every_language():
    for path in sorted((Path(backup_content.__file__).parent / "locales").glob("*.json")):
        strings = json.loads(path.read_text(encoding="utf-8"))["strings"]
        assert "telecode.sqlite" in strings["backup.import.warn.commands"] and "{count}" in strings["backup.import.warn.commands"]
        assert "telecode.sqlite" in strings["backup.import.choice"], path.name


# ---- CN2/CN3/CN4/CN7 ---------------------------------------------------------------------------------------------------


def test_is_link_recognises_links_and_junctions_and_not_plain_folders(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    link = tmp_path / "link"
    if not junction(link, plain):
        pytest.skip("no junctions or symbolic links here")
    assert jsontext._is_link(link) and not jsontext._is_link(plain)
    assert jsontext._is_link(tmp_path / "does-not-exist")  # a path that cannot be looked at is not touched


def test_a_link_with_a_marker_behind_it_is_never_handed_to_rmtree(tmp_path, monkeypatch):
    """Shows that the decision is ``_is_link``: ``shutil.rmtree`` itself refuses a junction since Python 3.8, so
    the folder surviving would prove nothing; what is observed is that the call is never made for the link."""
    victim = make_scratch(tmp_path, "victim", True, 30)
    parent = tmp_path / "tmp"
    parent.mkdir()
    link = parent / "mourse-backup-link"
    if not junction(link, victim):
        pytest.skip("no junctions or symbolic links here")
    real = make_scratch(parent, "mourse-backup-real", True, 3)
    removed = []
    original = shutil.rmtree
    monkeypatch.setattr(jsontext.shutil, "rmtree", lambda path, *a, **k: removed.append(Path(path)) or original(path, *a, **k))
    jsontext.clean_orphaned_scratch(parent=parent)
    assert removed == [real] and (victim / "settings.json").is_file() and os.path.lexists(link)


def test_a_scratch_folder_whose_marker_cannot_be_written_is_removed(tmp_path, monkeypatch):
    made = []
    real_mkdtemp = backup.tempfile.mkdtemp
    monkeypatch.setattr(backup.tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(dir=tmp_path, **kw)) or made[-1])
    real_write = Path.write_text

    def refuse(self, *args, **kwargs):
        if self.name == jsontext.SCRATCH_MARKER:
            raise OSError("disk full")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse)
    with pytest.raises(OSError):
        with backup._scratch():
            pass
    assert len(made) == 1 and not Path(made[0]).exists() and not backup._live_scratch & {Path(made[0])}


def test_an_undeliverable_startup_warning_goes_through_the_real_gui_path(monkeypatch):
    from mourse_decoder.ui import tray as tray_mod

    shown, scheduled = [], []
    tray = object.__new__(tray_mod.TrayApp)
    tray._ui_queue = queue.Queue()
    tray._root = SimpleNamespace(after=lambda ms, func: scheduled.append(func))
    tray._hook_error, tray._hook_error_shown = None, False
    tray._controller = SimpleNamespace(
        poll_active_toggle=lambda: None, poll_cycle=lambda: 0, poll_undo=lambda: 0, poll_function_keys=lambda: None,
        check_calibration_timeout=lambda: False,
    )

    def notify(message, title):
        if "bad" in message:
            raise ValueError("string too long")
        shown.append(message)

    tray._icon = SimpleNamespace(notify=notify)
    app.deliver_warnings(tray.on_error, ["first", "bad one", "last"])  # queues, like the start does
    tray._drain_queue()  # the Tk thread runs them: the failing one is logged, the others are shown
    assert shown == ["first", "last"] and len(scheduled) == 1  # and the loop goes on


def test_commands_in_texts_has_no_unused_switch():
    assert list(inspect.signature(macros.commands_in_texts).parameters) == ["texts"]


# ---- older findings -----------------------------------------------------------------------------------------------


def test_the_tooltip_is_cut_by_utf16_units_so_127_emoji_do_not_raise():
    from mourse_decoder.ui import tray as tray_mod

    tray = object.__new__(tray_mod.TrayApp)
    tray._icon = SimpleNamespace(title="")
    tray_mod.TrayApp._set_title(tray, "\U0001f600" * 127)
    assert len(tray._icon.title.encode("utf-16-le")) // 2 <= 127
    tray_mod.TrayApp._set_title(tray, "short")
    assert tray._icon.title == "short"


def test_write_atomic_with_an_unpaired_surrogate_fails_cleanly_and_leaves_nothing(tmp_path):
    target = tmp_path / "x.json"
    with pytest.raises(ValueError):
        jsontext.write_atomic(target, "a\ud800")
    assert list(tmp_path.iterdir()) == []
    jsontext.write_atomic(target, "fine")
    assert target.read_text() == "fine" and [p.name for p in tmp_path.iterdir()] == ["x.json"]


@pytest.mark.skipif(sys.version_info < (3, 10), reason="the project supports 3.10 and up")
def test_the_new_code_uses_nothing_newer_than_python_3_10():
    source = "\n".join(
        (Path(backup_content.__file__).parent / name).read_text(encoding="utf-8")
        for name in ("jsontext.py", "settings.py", "backup.py", "app.py")
    )
    assert "is_junction" not in source and "isjunction" not in source
