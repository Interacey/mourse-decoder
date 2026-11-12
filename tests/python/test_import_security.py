"""What a backup (or a data file in it) can make the program type: telecode text, word separators, big files.

Everything the decoders output runs through the macro parser unless it is marked plain. These tests cover the
ways a bundle could get a command such as ``{Win+R}`` into that path without the import dialog saying so.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest
from mourse_decoder import app, backup, backup_content, jsontext, macros
from mourse_decoder.controller import MorseController
from mourse_decoder.decoders import Emission, TelecodeDecoder
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.i18n import set_language
from mourse_decoder.settings import SettingsStore
from mourse_decoder.telecode import TelecodeStore
from test_backup import make_config

EVIL = "{Win+R}calc{Enter}"
ALPHABET_DIR = Path(backup_content.__file__).parent / "alphabets"
SMILEY_DIR = Path(backup_content.__file__).parent / "smiley_sets"


def make_bundle(path: Path, files: dict[str, bytes], extra_manifest: dict | None = None) -> Path:
    entries = [{"path": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for n, d in files.items()]
    manifest = {"format": backup.FORMAT_NAME, "format_version": backup.FORMAT_VERSION, "files": entries}
    manifest.update(extra_manifest or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def make_telecode_db(path: Path, rows: dict[str, str]) -> bytes:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE telecode(code TEXT PRIMARY KEY, char TEXT NOT NULL) WITHOUT ROWID")
    conn.executemany("INSERT INTO telecode VALUES(?, ?)", rows.items())
    conn.commit()
    conn.close()
    return path.read_bytes()


def digit_sequences() -> dict[str, str]:
    codes = DictionaryManager().load("common").codes
    return {digit: code for code, digit in codes.items() if digit.isdigit()}


# ---- S1: telecode ----------------------------------------------------------------------------------------------


def test_telecode_text_is_typed_as_it_is_and_never_read_as_a_macro(tmp_path, fake_engine, injector):
    db = tmp_path / "telecode.sqlite"
    make_telecode_db(db, {"0001": EVIL, "0002": "a\nb\tc"})
    controller = MorseController(
        fake_engine, injector, DictionaryManager(telecode_db=db),
        SettingsStore(tmp_path).load().replace(alphabet="chinese-telecode"), use_hook=False,
    )
    controller.start()
    seq = digit_sequences()
    fake_engine.letters(*(seq[d] for d in "0001"))
    assert not [c for c in injector.calls if c[0] == "keys"], "a key combination was pressed"
    assert injector.buffer.endswith(EVIL)  # the braces came out as characters
    injector.buffer = ""
    fake_engine.letters(*(seq[d] for d in "0002"))
    assert injector.buffer.endswith("abc")  # no Enter or Tab from the control characters


def test_telecode_emission_is_plain_and_equal_to_a_normal_one(telecode_db):
    store = TelecodeStore(telecode_db)
    decoder = TelecodeDecoder(store, DictionaryManager().load("common").codes)
    seq = digit_sequences()
    emission = None
    for digit in "0022":
        emission = decoder.decode(seq[digit])
    assert emission is not None and emission.plain and emission.erase == 3
    assert emission == Emission(emission.text, erase=3)  # ``plain`` changes how, not what
    store.close()


def evil_telecode_bundle(tmp_path: Path) -> tuple[Path, Path]:
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    data = make_telecode_db(tmp_path / "t.sqlite", {"0001": EVIL, "0002": "中"})
    return make_bundle(tmp_path / "evil.zip", {"telecode.sqlite": data}), config


def test_a_telecode_database_with_commands_is_reported_and_left_out_without_macros(tmp_path):
    zip_path, config = evil_telecode_bundle(tmp_path)
    preview = backup.inspect_bundle(zip_path, config)
    assert preview.risky and preview.risky_files == ("telecode.sqlite",)
    found = [c for c in preview.commands if c.path == "telecode.sqlite"]
    assert found and found[0].code == "0001" and "{Win+R}" in found[0].risks
    assert preview.safe is not None and not preview.safe.risky_files
    assert all(e.path != "telecode.sqlite" for e in preview.safe.entries)  # "import without macros" leaves it out


def test_a_normal_telecode_database_raises_no_warning(tmp_path, telecode_db):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    zip_path = make_bundle(tmp_path / "ok.zip", {"telecode.sqlite": Path(telecode_db).read_bytes()})
    preview = backup.inspect_bundle(zip_path, config)
    assert not preview.risky and not preview.commands and preview.risky_files == ()


def test_a_telecode_database_that_runs_into_the_time_limit_is_refused(tmp_path, monkeypatch):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    rows = {f"{i:04d}": "x" for i in range(10_000)}
    zip_path = make_bundle(tmp_path / "big.zip", {"telecode.sqlite": make_telecode_db(tmp_path / "t.sqlite", rows)})
    monkeypatch.setattr(backup, "SQLITE_CHECK_SECONDS", -1)  # every progress check is already past the deadline
    with pytest.raises(backup.BackupError):
        backup.inspect_bundle(zip_path, config)


# ---- S2: word separator ----------------------------------------------------------------------------------------


def alphabet_bytes(codes: dict[str, str], **extra: object) -> bytes:
    return json.dumps({"id": "evil", "name": "evil", "extends": "latin", "codes": codes, **extra}).encode()


def test_the_word_separator_is_checked_like_a_code(tmp_path):
    data = alphabet_bytes({"...-.-": "x"}, word_separator=EVIL)
    assert backup_content.commands_per_code(data)[0][0] == "word_separator"
    path = tmp_path / "evil.json"
    path.write_bytes(data)
    assert "{Win+R}" in macros.commands_in_file(path)  # the single-file import asks too
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    zip_path = make_bundle(tmp_path / "b.zip", {"alphabets/evil.json": data})
    preview = backup.inspect_bundle(zip_path, config)
    assert preview.risky_files == ("alphabets/evil.json",)
    assert preview.commands[0].code == "word_separator"


def test_normal_separators_and_every_shipped_file_give_no_warning(tmp_path):
    for sep in (" ", "", "·", "。", "　"):
        data = alphabet_bytes({"...-.-": "x"}, word_separator=sep)
        assert backup_content.commands_per_code(data) == []
    shipped = [*ALPHABET_DIR.glob("*.json"), *SMILEY_DIR.glob("*.json")]
    assert len(shipped) >= 6
    for path in shipped:
        assert backup_content.commands_per_code(path.read_bytes()) == [], path.name
        assert macros.commands_in_file(path) == [], path.name


def test_only_codes_and_the_separator_are_typed_by_the_decoder():
    """The fields of a file that end up in output; ``combining`` forms one character with the letter before it."""
    from mourse_decoder.decoders import AlphabetDecoder

    alphabet = DictionaryManager().load("wabun")
    decoder = AlphabetDecoder(alphabet)
    assert decoder.decode(next(iter(alphabet.combining))) is None  # no base letter: nothing typed
    gap = decoder.word_gap()
    assert gap is None or gap.text == alphabet.word_separator


# ---- S3: no limit on the number of codes -----------------------------------------------------------------------


def filler_codes(n: int) -> dict[str, str]:
    return {format(i + 1, "b").replace("0", ".").replace("1", "-"): "x" for i in range(n)}


@pytest.mark.parametrize("fillers", [19_999, 20_000, 20_001, 20_500, 60_000])
def test_a_command_behind_many_harmless_codes_is_still_found(fillers):
    codes = filler_codes(fillers)
    codes["-" * 20 + "."] = EVIL  # the last entry
    data = alphabet_bytes(codes)
    assert len(data) <= backup.MAX_USER_JSON_BYTES
    started = time.monotonic()
    found = backup_content.commands_per_code(data)
    assert [text for _, text, _ in found] == [EVIL]
    assert time.monotonic() - started < 5


def test_a_big_alphabet_with_a_command_at_the_end_is_risky_in_an_import(tmp_path):
    codes = filler_codes(20_500)
    codes["-" * 20 + "."] = EVIL
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    zip_path = make_bundle(tmp_path / "b.zip", {"alphabets/evil.json": alphabet_bytes(codes)})
    assert backup.inspect_bundle(zip_path, config).risky_files == ("alphabets/evil.json",)


def test_the_list_kept_for_one_file_is_capped_but_the_file_is_risky():
    codes = {code: EVIL for code in filler_codes(backup_content.MAX_COMMANDS_LISTED + 50)}
    found = backup_content.commands_per_code(alphabet_bytes(codes))
    assert len(found) == backup_content.MAX_COMMANDS_LISTED


# ---- S4: restore points --------------------------------------------------------------------------------------


def test_only_a_restore_point_next_to_the_config_folder_removes_files(tmp_path):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    source = make_config(tmp_path / "src")
    (source / "alphabets" / "neu.json").write_text(
        json.dumps({"id": "neu", "name": "Neu", "codes": {".": "e"}}), encoding="utf-8"
    )
    backup.export_bundle(source, tmp_path / "in.zip")
    backup.import_bundle(tmp_path / "in.zip", config)
    assert (config / "alphabets" / "neu.json").is_file()
    restore_point = next(config.parent.glob("MourseDecoder-backup-*.zip"))
    assert "remove" in [e.status for e in backup.inspect_bundle(restore_point, config).entries]  # genuine: undo
    elsewhere = tmp_path / "Downloads"
    elsewhere.mkdir()
    moved = elsewhere / restore_point.name
    moved.write_bytes(restore_point.read_bytes())
    assert "remove" not in [e.status for e in backup.inspect_bundle(moved, config).entries]
    renamed = config.parent / "mine.zip"
    renamed.write_bytes(restore_point.read_bytes())
    assert "remove" not in [e.status for e in backup.inspect_bundle(renamed, config).entries]
    backup.import_bundle(moved, config)
    assert (config / "alphabets" / "neu.json").is_file()  # the forged copy deleted nothing


def test_a_forged_restore_point_cannot_delete_the_settings(tmp_path):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    forged = make_bundle(
        tmp_path / "x.zip", {"alphabets/a.json": b'{"id": "a", "codes": {".": "e"}}'},
        {"restore_point": True, "restore_created": ["settings.json", "function_keys.json"]},
    )
    result = backup.import_bundle(forged, config)
    assert result.removed == () and (config / "settings.json").is_file() and (config / "function_keys.json").is_file()


# ---- S5: leftovers of a killed process ----------------------------------------------------------------------


def test_old_scratch_folders_of_this_program_are_removed_and_nothing_else(tmp_path):
    old_ours, new_ours, old_foreign = (tmp_path / n for n in ("mourse-backup-old", "mourse-backup-new", "other-old"))
    old_file = tmp_path / "mourse-backup-file"
    for folder in (old_ours, new_ours, old_foreign):
        folder.mkdir()
        (folder / "settings.json").write_text("{}")
    old_file.write_text("x")
    two_days = time.time() - 2 * 86400
    for path in (old_ours, old_foreign, old_file):
        os.utime(path, (two_days, two_days))
    removed = jsontext.clean_orphaned_scratch(parent=tmp_path)
    assert removed == [old_ours]
    assert not old_ours.exists() and new_ours.exists() and old_foreign.exists() and old_file.exists()


# ---- S6: what a notice shows of an invalid id -------------------------------------------------------------


def test_a_notice_writes_control_and_direction_characters_out(tmp_path):
    set_language("en")
    (tmp_path / "settings.json").write_text(
        json.dumps({"alphabet": "a‮b\x1b[31mc\nd"}), encoding="utf-8"
    )
    store = SettingsStore(tmp_path)
    store.load()
    (message,) = app.settings_warnings(store)
    assert "‮" not in message and "\x1b" not in message and "\n" not in message
    assert "\\u202e" in message and "\\u001b" in message
