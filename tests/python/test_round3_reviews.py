"""Third review round: control characters, text from files in dialogs, surrogates, scratch folders, restore points."""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest
from mourse_decoder import app, backup, backup_content, clipboard, jsontext, macros
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.function_keys import FunctionKeysStore
from mourse_decoder.i18n import set_language
from mourse_decoder.settings import Settings, SettingsStore
from test_backup import make_config
from test_import_security import ALPHABET_DIR, SMILEY_DIR, alphabet_bytes, filler_codes, make_bundle, make_telecode_db
from test_integration_interactions import import_panel  # noqa: F401 - a fixture

PACKAGE = Path(backup_content.__file__).parent
LOCALES = PACKAGE / "locales"
EVIL = "{Win+R}calc{Enter}"
CONTROLS = [chr(c) for c in [*range(0x20), *range(0x7F, 0xA0)]]


# ---- SR1: every control character is a key event ------------------------------------------------------------------


@pytest.mark.parametrize("char", CONTROLS, ids=lambda c: f"U+{ord(c):04X}")
def test_every_control_character_in_a_text_is_reported(char):
    found = macros.commands_in_texts([f"a{char}b"], windows=True)
    expected = {"\n": "{Enter}", "\r": "{Enter}", "\t": "{Tab}"}.get(char, f"{{control character U+{ord(char):04X}}}")
    assert found == [expected]


@pytest.mark.parametrize("char", ["\b", "\x03", "\x16", "\x18", "\x1a", "\x1b", "\x7f", "\x85"])
def test_a_control_character_is_found_by_every_check_and_left_out_without_macros(tmp_path, char):
    codes = {".-": f"a{char}"}
    data = alphabet_bytes(codes, word_separator=char)
    assert backup_content.commands_per_code(data)  # a code of a bundle
    path = tmp_path / "x.json"
    path.write_bytes(data)
    assert macros.commands_in_file(path)  # the single-file import
    assert macros.commands_in_texts([f"x{char}"], windows=True)  # a function key macro (backup.slot_commands)
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    smiley = json.dumps({"id": "ctl", "codes": {"..--": f"x{char}"}}).encode()
    telecode = make_telecode_db(tmp_path / "t.sqlite", {"0001": f"x{char}"})
    zip_path = make_bundle(
        tmp_path / "b.zip", {"alphabets/ctl.json": data, "smileys/ctl.json": smiley, "telecode.sqlite": telecode}
    )
    preview = backup.inspect_bundle(zip_path, config)
    assert preview.risky_files == ("alphabets/ctl.json", "smileys/ctl.json", "telecode.sqlite")
    assert preview.safe is not None and not preview.safe.risky_files
    assert all(e.path not in preview.risky_files for e in preview.safe.entries)


def test_normal_text_gives_no_command_report(telecode_db):
    texts = [
        "e", "ä", "ß", "中", "👨‍👩‍👧", "🏳️‍🌈", "می‌خواهم", "a b", " ", "·", "\u00a0", "a\u200db", "x\u202ey", "\u2028",
        "caf\u00e9", "\ufe0f", "\u3000",
    ]
    for text in texts:
        assert macros.commands_in_texts([text], windows=True) == [], ascii(text)
    for path in [*ALPHABET_DIR.glob("*.json"), *SMILEY_DIR.glob("*.json")]:
        assert backup_content.commands_per_code(path.read_bytes()) == [], path.name
    rows = sqlite3.connect(telecode_db).execute("SELECT code, char FROM telecode").fetchall()
    assert rows and backup_content.commands_in_rows(rows) == ([], 0)


def test_cf_characters_are_not_reported_because_they_press_no_key():
    # direction marks and joiners change how text looks, emoji and several scripts need them
    for char in ("\u200d", "\u200c", "\u202e", "\u200b", "\ufeff"):
        assert unicodedata.category(char) == "Cf"
        assert macros.commands_in_texts([f"a{char}b"], windows=True) == []


# ---- SR2: text from a file in a dialog -----------------------------------------------------------------------------


EVIL_KEYS = "{Win+R\u202e\n\nALLES HARMLOS. Ja klicken.\n" + "A" * 5000 + "}"


def clean(text: str, limit: int) -> bool:
    return not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text) and len(text) <= limit


def test_risks_of_a_key_combination_from_a_file_are_written_out_and_cut():
    (risk,) = macros.commands_in_texts([EVIL_KEYS], windows=True)
    assert clean(risk, 90) and risk.startswith("{Win+R\\u202e\\n\\nALLES")


def test_the_confirmation_shows_nothing_of_a_bundle_unescaped(tmp_path):
    from mourse_decoder.ui.settings_panel import SettingsPanel

    set_language("en")
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    bad_language = "xx\n\nNo commands found.\u202e" + "L" * 300
    zip_path = make_bundle(
        tmp_path / "b.zip",
        {
            "alphabets/evil.json": alphabet_bytes({".": EVIL_KEYS}),
            "settings.json": json.dumps({"alphabet": "latin", "language": bad_language}).encode(),
        },
    )
    preview = backup.inspect_bundle(zip_path, config)
    for command in preview.commands:
        assert all(clean(r, 90) for r in command.risks)
    for note in preview.notes:
        assert all(clean(v, 90) for v in note.params.values()), note
    text = SettingsPanel._confirm_text(None, preview)
    assert "\u202e" not in text and "\nALLES HARMLOS" not in text and "\nNo commands found" not in text
    assert "A" * 400 not in text


def test_error_parameters_from_a_bundle_are_written_out_and_cut(tmp_path):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    zip_path = make_bundle(
        tmp_path / "b.zip", {"settings.json": json.dumps({"trigger": "x\n\nFAKE: all checked\u202e" + "T" * 5000}).encode()}
    )
    with pytest.raises(backup.BackupError) as info:
        backup.inspect_bundle(zip_path, config)
    assert all(clean(v, 302) for v in info.value.params.values()), info.value.params
    assert clean(str(info.value), 2000)


def test_the_single_file_import_dialog_is_written_out(tmp_path, monkeypatch):
    from tkinter import messagebox

    from mourse_decoder.ui import theme

    path = tmp_path / "a\u202eb.json"
    path.write_bytes(alphabet_bytes({".": EVIL_KEYS}))
    shown = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, message, **kw: shown.append(message) or False)
    assert theme.confirm_commands(None, path) is False
    assert shown and "\u202e" not in shown[0] and "\n\nALLES" not in shown[0] and "A" * 400 not in shown[0]


def test_the_single_file_import_lists_waitwindow_like_a_bundle(tmp_path):
    path = tmp_path / "w.json"
    path.write_bytes(alphabet_bytes({".": "{WaitWindow Run}"}))
    assert macros.commands_in_file(path) == ["{WaitWindow …}"]
    assert [c[2] for c in backup_content.commands_per_code(path.read_bytes())] == [("{WaitWindow …}",)]


# ---- SR3: lone surrogates -------------------------------------------------------------------------------------------


def test_fit_balloon_survives_a_lone_surrogate_and_counts_units():
    from mourse_decoder.ui.tray import BALLOON_TITLE_UNITS, fit_balloon

    assert fit_balloon("a\ud800b") == "a\ufffdb"
    long = "x" * 300 + "\ud800"
    assert len(fit_balloon(long)) <= 255
    title = fit_balloon("x" * 62 + "\U0001f600" + "y", BALLOON_TITLE_UNITS)  # the pair would be units 63 and 64
    assert title == "x" * 62 + "…"


def test_the_balloon_title_is_cut_too(monkeypatch):
    from mourse_decoder.ui import tray as tray_mod

    shown = []
    tray = object.__new__(tray_mod.TrayApp)
    tray._ui_queue = queue.Queue()
    tray._icon = SimpleNamespace(notify=lambda message, title: shown.append((message, title)))
    monkeypatch.setattr(tray_mod, "tr", lambda key, **kw: "T" * 100)
    tray.on_error("boom\ud800")
    tray._ui_queue.get_nowait()()
    (message, title), = shown
    assert message == "boom\ufffd" and len(title) <= 64


def test_a_warning_that_cannot_be_shown_never_stops_the_start():
    shown = []

    def show(warning):
        if "bad" in warning:
            raise UnicodeEncodeError("utf-16", warning, 0, 1, "surrogates not allowed")
        shown.append(warning)

    app.deliver_warnings(show, ["first", "bad one", "last"])
    assert shown == ["first", "last"]


def test_function_keys_with_a_lone_surrogate_start_the_program_and_say_so(tmp_path):
    from mourse_decoder.ui import tray as tray_mod

    (tmp_path / "function_keys.json").write_bytes(
        b'{"schema_version":1,"slots":[{"slot":1,"trigger":"\\ud800","macro":"x","enabled":true},'
        b'{"slot":2,"trigger":"Ctrl+F5","macro":"{Win+\\ud800}","enabled":true}]}'
    )
    _, warnings = FunctionKeysStore(tmp_path).load(lambda combo: None)
    assert warnings
    tray = object.__new__(tray_mod.TrayApp)
    tray._ui_queue = queue.Queue()
    shown = []
    tray._icon = SimpleNamespace(notify=lambda message, title: shown.append(message))
    app.deliver_warnings(tray.on_error, warnings)  # what main() does
    while not tray._ui_queue.empty():
        tray._ui_queue.get_nowait()()
    assert len(shown) == len(warnings)
    assert not any(0xD800 <= ord(c) <= 0xDFFF for message in shown for c in message)


def test_a_macro_with_a_lone_surrogate_is_refused_before_anything_is_typed():
    for text in ("a\ud800", "{Win+\ud800}", "\udfff"):
        with pytest.raises(macros.MacroError):
            macros.parse(text, lambda combo: None)


def test_a_bundle_file_with_a_lone_surrogate_is_refused(tmp_path):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    for name, data in (
        ("function_keys.json", b'{"schema_version":1,"slots":[{"slot":1,"trigger":"\\ud800","macro":"x"}]}'),
        ("alphabets/s.json", b'{"id":"s","extends":"latin","codes":{".-.-.-":"\\ud800"}}'),
        ("settings.json", b'{"language":"\\udfff"}'),
    ):
        with pytest.raises(backup.BackupError) as info:
            backup.inspect_bundle(make_bundle(tmp_path / "b.zip", {name: data}), config)
        assert info.value.code in ("file_unusable", "function_keys_invalid"), (name, info.value.code)
    pair = b'{"id":"p","extends":"latin","codes":{".-.-.-":"\\ud83d\\ude00"}}'  # a real pair is fine
    assert backup.inspect_bundle(make_bundle(tmp_path / "ok.zip", {"alphabets/p.json": pair}), config)


def test_the_clipboard_encoding_does_not_raise_on_a_lone_surrogate(monkeypatch):
    put = []

    class Dummy:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(clipboard, "_Open", Dummy, raising=False)
    monkeypatch.setattr(clipboard, "_put", lambda fmt, data: put.append(data), raising=False)
    monkeypatch.setattr(clipboard, "_user32", SimpleNamespace(
        EmptyClipboard=lambda: None, RegisterClipboardFormatW=lambda name: 1), raising=False)
    clipboard.set_text("a\ud800")
    clipboard.set_files(["C:\\x\ud800"])
    assert len(put) == 3


# ---- SR4: scratch folders ---------------------------------------------------------------------------------------


def make_scratch(parent: Path, name: str, marker: bool, age_days: float) -> Path:
    folder = parent / name
    folder.mkdir()
    (folder / "settings.json").write_text("{}")
    if marker:
        (folder / jsontext.SCRATCH_MARKER).write_text("x")
    old = time.time() - age_days * 86400
    os.utime(folder, (old, old))
    return folder


def test_only_old_folders_with_the_marker_are_removed(tmp_path):
    ours_old = make_scratch(tmp_path, "mourse-backup-abc", True, 3)
    ours_new = make_scratch(tmp_path, "mourse-backup-new", True, 0)
    unpacked_export = make_scratch(tmp_path, "mourse-backup-20260101", False, 30)  # the user's own folder
    foreign = make_scratch(tmp_path, "other-abc", True, 30)
    assert jsontext.clean_orphaned_scratch(parent=tmp_path) == [ours_old]
    assert not ours_old.exists() and ours_new.exists() and unpacked_export.exists() and foreign.exists()


def test_a_real_scratch_folder_has_the_marker_and_is_cleaned_when_old(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.tempfile, "tempdir", str(tmp_path))
    with backup._scratch() as folder:
        assert (folder / jsontext.SCRATCH_MARKER).is_file() and folder.parent == tmp_path
        old = time.time() - 3 * 86400
        os.utime(folder, (old, old))
        assert jsontext.clean_orphaned_scratch(parent=tmp_path) == [folder]
    assert not folder.exists()


def junction(link: Path, target: Path) -> bool:
    """A real junction (Windows) or a symbolic link; False if neither can be made here."""
    if sys.platform == "win32":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return result.returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return True


def test_a_junction_is_never_followed_or_removed(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    parent = tmp_path / "tmp"
    parent.mkdir()
    old = time.time() - 3 * 86400
    os.utime(victim, (old, old))
    (victim / jsontext.SCRATCH_MARKER).write_text("x")  # even a marker behind the link must not count
    root_link = parent / "mourse-backup-link"
    if not junction(root_link, victim):
        pytest.skip("no junctions or symbolic links here")
    real = make_scratch(parent, "mourse-backup-real", True, 3)
    inner = real / "inner"
    assert junction(inner, victim)
    os.utime(real, (old, old))  # making the link changed the folder's time
    removed = jsontext.clean_orphaned_scratch(parent=parent)
    assert removed == [real] and not real.exists()
    assert (victim / "keep.txt").is_file() and os.path.lexists(root_link)  # nothing behind a link was touched


# ---- SR6: settings.json that changes the keys -----------------------------------------------------------------------


def test_a_note_says_when_settings_json_changes_keys_or_blocking(tmp_path):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    SettingsStore(config).save(Settings(alphabet="latin", trigger="ScrollLock"))
    changed = SettingsStore(tmp_path / "other")
    changed.save(Settings(alphabet="latin", trigger="F8", block_input=True, undo_shortcut="Ctrl+Alt+F9"))
    zip_path = make_bundle(tmp_path / "b.zip", {"settings.json": (tmp_path / "other" / "settings.json").read_bytes()})
    notes = [n for n in backup.inspect_bundle(zip_path, config).notes if n.code == "settings_keys"]
    assert len(notes) == 1
    assert notes[0].params["fields"] == "trigger, block_input, undo_shortcut"


def test_no_note_for_the_own_backup_a_fresh_install_or_other_changes(tmp_path):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    SettingsStore(config).save(Settings(alphabet="latin", trigger="F8", block_input=True))
    own = tmp_path / "own.zip"
    backup.export_bundle(config, own)
    other = tmp_path / "other"
    SettingsStore(other).save(Settings(alphabet="greek", trigger="F8", block_input=True, letter_gap_ms=900))
    different = make_bundle(tmp_path / "d.zip", {"settings.json": (other / "settings.json").read_bytes()})
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    for zip_path, folder in ((own, config), (different, config), (different, fresh)):
        assert not [n for n in backup.inspect_bundle(zip_path, folder).notes if n.code == "settings_keys"]


def test_the_import_texts_name_telecode_and_the_new_note_in_every_language():
    for path in sorted(LOCALES.glob("*.json")):
        strings = json.loads(path.read_text(encoding="utf-8"))["strings"]
        assert "telecode.sqlite" in strings["backup.import.choice"], path.name
        assert "telecode.sqlite" in strings["backup.import.warn.commands"], path.name
        assert "{fields}" in strings["backup.warn.settings_keys"], path.name


# ---- CR-M2 / CR-L6: restore points ----------------------------------------------------------------------------


def test_a_restore_point_next_to_a_config_folder_that_is_a_junction_still_undoes_the_import(tmp_path):
    real = make_config(tmp_path / "real" / "Target")
    links = tmp_path / "links"
    links.mkdir()
    config = links / "MourseDecoder"
    if not junction(config, real):
        pytest.skip("no junctions or symbolic links here")
    source = make_config(tmp_path / "src")
    (source / "alphabets" / "neu.json").write_text(
        json.dumps({"id": "neu", "name": "Neu", "codes": {".": "e"}}), encoding="utf-8"
    )
    backup.export_bundle(source, tmp_path / "in.zip")
    backup.import_bundle(tmp_path / "in.zip", config)
    (restore_point,) = links.glob("MourseDecoder-backup-*.zip")  # written next to the link, where the program looks
    statuses = [e.status for e in backup.inspect_bundle(restore_point, config).entries]
    assert "remove" in statuses
    backup.import_bundle(restore_point, config)
    assert not (real / "alphabets" / "neu.json").exists()


def test_the_restore_point_check_uses_the_manifest_that_was_checked_not_the_file_again(tmp_path, monkeypatch):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    source = make_config(tmp_path / "src")
    (source / "alphabets" / "neu.json").write_text(
        json.dumps({"id": "neu", "name": "Neu", "codes": {".": "e"}}), encoding="utf-8"
    )
    backup.export_bundle(source, tmp_path / "in.zip")
    backup.import_bundle(tmp_path / "in.zip", config)
    (restore_point,) = config.parent.glob("MourseDecoder-backup-*.zip")
    calls = []
    original = backup._is_restore_point
    monkeypatch.setattr(backup, "_is_restore_point", lambda path: calls.append(path) or original(path))
    assert "remove" in [e.status for e in backup.inspect_bundle(restore_point, config).entries]
    assert calls == []


# ---- CR-M3 / CR-L1: every way to choose an alphabet or smiley set -----------------------------------------------------


def test_the_alphabet_and_smiley_pages_pass_the_choice_on(tk_root, fake_engine, injector):
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.alphabet_panel import AlphabetPanel
    from mourse_decoder.ui.smiley_panel import SmileyPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    dicts = DictionaryManager()
    controller = MorseController(fake_engine, injector, dicts, Settings(), use_hook=False)
    controller.start()
    calls = []
    apply = lambda settings, **kwargs: calls.append((settings, kwargs))  # noqa: E731 - what TrayApp._apply accepts
    alphabets = AlphabetPanel(tk_root, controller, dicts, apply, on_changed=lambda: None)
    smileys = SmileyPanel(tk_root, controller, dicts, apply, on_changed=lambda: None)
    try:
        alphabets._selected = next(a for a in dicts.available() if a.id == "greek")
        alphabets._activate()
        assert calls[-1][0].alphabet == "greek" and calls[-1][1] == {"chosen": ("alphabet",)}
        entries = [None, *dicts.smiley_sets()]
        smileys._entries, smileys._index = entries, 0
        smileys._use()
        assert calls[-1][0].smileys == "" and calls[-1][1] == {"chosen": ("smileys",)}
        smileys._index = 1
        smileys._use()
        assert calls[-1][0].smileys == entries[1].id and calls[-1][1] == {"chosen": ("smileys",)}
    finally:
        alphabets.destroy()
        smileys.destroy()


def test_a_box_chosen_during_a_backup_job_is_still_saved_as_chosen_afterwards(import_panel):  # noqa: F811
    page, *_ = import_panel('{"alphabet": "german"}', '{"alphabet": "german"}')
    seen = []
    page._changed = lambda chosen=(): seen.append(chosen)  # what _finish calls after the job
    page._backup_running = True
    SettingsPanelClass = type(page)
    SettingsPanelClass._changed(page, chosen=("alphabet",))  # picked while the job runs
    SettingsPanelClass._changed(page, chosen=("smileys",))
    assert page._deferred_change and page._deferred_chosen == {"alphabet", "smileys"}
    page._holds_gate = False
    SettingsPanelClass._finish(page)
    assert seen == [("alphabet", "smileys")] and not page._deferred_chosen


# ---- CR-L2: a failed change puts the smileys back ----------------------------------------------------------------


def test_a_failed_alphabet_change_with_a_broken_smiley_set_leaves_the_smileys_as_they_were(
    fake_engine, injector, tmp_path
):
    user = tmp_path / "smileys"
    user.mkdir()
    for name, code in (("a", "........-"), ("b", "........--")):
        (user / f"{name}.json").write_text(json.dumps({"id": name, "name": name, "codes": {code: "Z"}}), encoding="utf-8")
    dicts = DictionaryManager(user_smiley_directory=user)
    controller = MorseController(fake_engine, injector, dicts, Settings(smileys="a"), use_hook=False)
    controller.start()
    assert dicts.smiley_set_id == "a"
    (user / "a.json").unlink()  # the loaded set's file is gone: it cannot be read again
    with pytest.raises(Exception):  # noqa: B017 - the alphabet does not exist: the change is refused
        controller.apply_settings(controller.settings.replace(smileys="b", alphabet="gibts-nicht"))
    assert dicts.smiley_set_id == "a" and dicts.smiley_state()[1] == {"........-": "Z"}
    assert controller.settings.smileys == "a"


# ---- CR-L4 / CR-L5: size of the lists, one open of the database ------------------------------------------------------


def test_the_confirmation_names_how_many_entries_have_commands_not_only_how_many_are_listed(tmp_path):
    from mourse_decoder.ui.settings_panel import SettingsPanel

    set_language("en")
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    total = backup_content.MAX_COMMANDS_LISTED + 25
    codes = {code: EVIL for code in filler_codes(total)}
    zip_path = make_bundle(tmp_path / "b.zip", {"alphabets/big.json": alphabet_bytes(codes)})
    preview = backup.inspect_bundle(zip_path, config)
    assert len(preview.commands) == backup_content.MAX_COMMANDS_LISTED and preview.commands_total == total
    text = SettingsPanel._confirm_text(None, preview)
    assert f"WARNING: {total} entries" in text


def test_a_two_mib_alphabet_is_checked_once_and_fast(tmp_path, monkeypatch):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    codes = filler_codes(60_000)
    codes["-" * 20 + "."] = EVIL
    data = alphabet_bytes(codes)
    zip_path = make_bundle(tmp_path / "b.zip", {"alphabets/big.json": data})
    scans = []
    original = backup_content.typed_commands
    monkeypatch.setattr(backup_content, "typed_commands", lambda blob: scans.append(1) or original(blob))
    started = time.monotonic()
    preview = backup.inspect_bundle(zip_path, config)
    assert preview.risky_files == ("alphabets/big.json",)
    assert len(scans) == 1  # the check without macros leaves the file out: it is not read a second time
    assert time.monotonic() - started < 10


def test_a_telecode_database_is_written_and_opened_once_for_the_whole_check(tmp_path, monkeypatch):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    data = make_telecode_db(tmp_path / "t.sqlite", {"0001": EVIL, "0002": "中"})
    zip_path = make_bundle(tmp_path / "b.zip", {"telecode.sqlite": data})
    opened = []
    real_connect = backup.sqlite3.connect
    monkeypatch.setattr(backup.sqlite3, "connect", lambda *a, **k: opened.append(a) or real_connect(*a, **k))
    preview = backup.inspect_bundle(zip_path, config)
    assert preview.risky_files == ("telecode.sqlite",)
    assert len(opened) == 1  # the structure check and the scan of the rows share one open


# ---- CR-L7 / CR-L9: cutting -------------------------------------------------------------------------------------------


def test_display_text_never_cuts_a_written_out_character_in_the_middle():
    assert jsontext.display_text("\u202e" * 40, 16) == "\\u202e\\u202e…"
    assert jsontext.display_text("\U0001f600\x00" * 10, 14) == "\U0001f600\\u0000\U0001f600\\u0000…"
    assert jsontext.display_text("abcdef", 3) == "abc…" and jsontext.display_text("abc", 3) == "abc"


def test_the_notice_for_an_invalid_id_ends_on_a_whole_escape(tmp_path):
    set_language("en")
    (tmp_path / "settings.json").write_text(json.dumps({"alphabet": "\u202e" * 40}), encoding="utf-8")
    store = SettingsStore(tmp_path)
    store.load()
    (message,) = app.settings_warnings(store)
    assert "\\u20…" not in message and "\\u202e…" in message
