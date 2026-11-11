"""Hardening of the config backup: hostile bundles, honest failures, validation with the program's own loaders."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest
from mourse_decoder import backup
from mourse_decoder.backup import BackupError, export_bundle, import_bundle, inspect_bundle
from mourse_decoder.function_keys import FunctionKeys, FunctionKeysStore
from mourse_decoder.settings import Settings, SettingsStore
from test_backup import SQLITE, build_zip, make_config, snapshot

HOSTILE_MACRO = "{Win+R}{WaitWindow Run}powershell -w hidden -c calc{Enter}"


def keys_file(*slots: dict) -> bytes:
    return json.dumps({"schema_version": 1, "slots": list(slots)}).encode()


def slot(number: int, **fields) -> dict:
    return {"slot": number, "trigger": f"F{number}" if number >= 13 else "Ctrl+F5", "macro": "", "enabled": True,
            "swallow": True, **fields}


def check_like_the_app(combo: str) -> None:
    """The injector's check, as far as these tests need it: known modifiers, then one key."""
    *modifiers, key = (part.strip().lower() for part in combo.split("+"))
    if any(m not in {"ctrl", "alt", "shift", "win"} for m in modifiers) or not (len(key) == 1 or key in {"enter", "tab"}):
        raise ValueError(f"keine Tastenkombination: {combo!r}")


# 1. macros of a foreign bundle are shown, and function_keys.json can be left out


def test_the_preview_summarises_enabled_macros_and_flags_risky_commands(tmp_path):
    data = keys_file(
        slot(13, macro=HOSTILE_MACRO),
        slot(14, macro="plain text"),
        slot(15, macro="{File C:\\x}{Clipboard}"),
        slot(16, macro=HOSTILE_MACRO, enabled=False),  # off: never runs, not listed
        slot(17, macro=""),  # nothing to run
    )
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": data})
    preview = inspect_bundle(zip_path, tmp_path / "t")
    by_slot = {s.slot: s for s in preview.slots}
    assert sorted(by_slot) == [13, 14, 15]
    assert by_slot[13].macro == HOSTILE_MACRO and by_slot[13].trigger == "F13" and by_slot[13].enabled
    assert by_slot[13].risks == ("{Win+...}", "{WaitWindow}")
    assert by_slot[14].risks == ()
    assert by_slot[15].risks == ("{File}/{Folder}", "{Clipboard}")
    assert preview.risky


def test_the_macro_summary_is_cut_at_200_characters_and_escaped(tmp_path):
    macro = "a\u202eb\x07c\nd\u200be" + "x" * 300
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": keys_file(slot(13, macro=macro))})
    shown = inspect_bundle(zip_path, tmp_path / "t").slots[0].macro
    assert len(shown) == 201 and shown.endswith("…")
    assert shown.startswith("a\\u202eb\\u0007c\\nd\\u200be")
    assert not any(ord(c) < 32 or c in "\u202e\u200b" for c in shown)


def test_an_unchanged_function_keys_file_brings_no_summary(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    data = keys_file(slot(13, macro="hello"))
    (target / "function_keys.json").write_bytes(data)
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": data, "settings.json": b'{"alphabet": "greek"}'})
    assert inspect_bundle(zip_path, target).slots == ()


def test_import_without_function_keys_leaves_that_file_alone_and_does_not_validate_it(tmp_path):
    target = tmp_path / "t"
    make_config(target)
    own = (target / "function_keys.json").read_bytes()
    broken = b'{"slots": [1, 2'  # would fail validation
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"alphabet": "greek"}', "function_keys.json": broken})
    with pytest.raises(BackupError) as caught:
        inspect_bundle(zip_path, target)
    assert caught.value.path == "function_keys.json"
    preview = inspect_bundle(zip_path, target, exclude=["function_keys.json"])
    assert [e.path for e in preview.entries] == ["settings.json"]
    result = import_bundle(zip_path, target, exclude=["function_keys.json"], expected_sha256=preview.sha256)
    assert result.written == ("settings.json",)
    assert (target / "function_keys.json").read_bytes() == own


def test_a_hostile_function_keys_file_is_imported_unchanged_not_silently_disabled(tmp_path):
    data = keys_file(slot(13, macro=HOSTILE_MACRO))
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": data})
    target = tmp_path / "t"
    import_bundle(zip_path, target)
    assert (target / "function_keys.json").read_bytes() == data


# 2. a rollback that fails is reported


def test_a_failed_rollback_says_what_is_left_and_names_the_restore_point(tmp_path, monkeypatch):
    target = tmp_path / "t"
    make_config(target)
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"alphabet": "greek"}', "alphabets/a.json": b'{"id": "a"}'})
    real = backup._atomic_write
    calls: list[str] = []

    def failing(path, data):
        calls.append(path.name)
        if path.name == "a.json" or calls.count("settings.json") > 1:  # the write of a.json and the roll back of settings.json
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(backup, "_atomic_write", failing)
    with pytest.raises(BackupError) as caught:
        import_bundle(zip_path, target)
    message = str(caught.value)
    assert "left as it was" not in message
    assert "settings.json" in message and "could not be put back" in message
    restore = next(tmp_path.glob("t-backup-*.zip"))
    assert caught.value.restore_point == restore and str(restore) in message
    assert (target / "settings.json").read_text() == '{"alphabet": "greek"}'  # really was left in the imported state


def test_a_clean_rollback_still_says_the_folder_was_left_as_it_was(tmp_path, monkeypatch):
    target = tmp_path / "t"
    make_config(target)
    before = snapshot(target)
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"alphabet": "greek"}', "alphabets/a.json": b'{"id": "a"}'})
    real = backup._write_file

    def flaky(path, data):
        if path.name == "a.json":
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(backup, "_write_file", flaky)
    with pytest.raises(BackupError, match="left as it was") as caught:
        import_bundle(zip_path, target)
    assert caught.value.restore_point is not None
    assert snapshot(target) == before


# 3. nothing but BackupError comes out


def corrupt(zip_path: Path, name: str, how: str) -> Path:
    raw = bytearray(zip_path.read_bytes())
    with zipfile.ZipFile(zip_path) as z:
        info = z.getinfo(name)
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    if how == "deflate":
        for i in range(start, start + 12):
            raw[i] ^= 0xFF
    elif how == "method":  # compression method 99 in the local header and the central directory
        for sig, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
            pos = raw.find(sig)
            while pos != -1:
                raw[pos + offset : pos + offset + 2] = (99).to_bytes(2, "little")
                pos = raw.find(sig, pos + 1)
    elif how == "lzma":
        for sig, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
            pos = raw.find(sig)
            while pos != -1:
                raw[pos + offset : pos + offset + 2] = (14).to_bytes(2, "little")
                pos = raw.find(sig, pos + 1)
    zip_path.write_bytes(bytes(raw))
    return zip_path


@pytest.mark.parametrize("how", ["deflate", "method", "lzma"])
def test_damaged_or_unsupported_compression_is_a_backup_error(tmp_path, how):
    target = tmp_path / "t"
    make_config(target)
    before = snapshot(target)
    text = b'{"alphabet": "greek", "pad": "' + b"x" * 400 + b'"}'
    zip_path = corrupt(build_zip(tmp_path / "z.zip", {"settings.json": text}), "settings.json", how)
    with pytest.raises(BackupError, match="Cannot read"):
        inspect_bundle(zip_path, target)
    with pytest.raises(BackupError, match="Cannot read"):
        import_bundle(zip_path, target)
    assert snapshot(target) == before


def test_any_unexpected_error_in_inspect_and_import_becomes_a_backup_error(tmp_path, monkeypatch):
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b"{}"})

    def boom(*_a, **_k):
        raise ZeroDivisionError("surprise")

    monkeypatch.setattr(backup, "_load_bundle", boom)
    for call in (lambda: inspect_bundle(zip_path, tmp_path / "t"), lambda: import_bundle(zip_path, tmp_path / "t")):
        with pytest.raises(BackupError, match="ZeroDivisionError: surprise") as caught:
            call()
        assert isinstance(caught.value.__cause__, ZeroDivisionError)


def test_a_failure_after_the_restore_point_exists_names_it_and_changes_nothing(tmp_path, monkeypatch):
    target = tmp_path / "t"
    make_config(target)
    before = snapshot(target)
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"alphabet": "greek"}'})
    real_write_zip, real_read_bytes = backup._write_zip, Path.read_bytes
    state = {"restore_point": False}

    def write_zip(*args, **kwargs):
        result = real_write_zip(*args, **kwargs)
        state["restore_point"] = True
        return result

    def read_bytes(self):
        if state["restore_point"] and self == target / "settings.json":
            raise PermissionError("locked")
        return real_read_bytes(self)

    monkeypatch.setattr(backup, "_write_zip", write_zip)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(BackupError) as caught:
        import_bundle(zip_path, target)
    monkeypatch.undo()
    assert caught.value.restore_point is not None and caught.value.restore_point.exists()
    assert snapshot(target) == before


def test_a_non_oserror_during_the_write_is_wrapped_and_rolled_back(tmp_path, monkeypatch):
    target = tmp_path / "t"
    make_config(target)
    before = snapshot(target)
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"alphabet": "greek"}', "alphabets/a.json": b"{}"})
    real = backup._write_file

    def flaky(path, data):
        if path.name == "a.json":
            raise RuntimeError("odd")
        real(path, data)

    monkeypatch.setattr(backup, "_write_file", flaky)
    with pytest.raises(BackupError, match="RuntimeError|odd") as caught:
        import_bundle(zip_path, target)
    assert caught.value.restore_point is not None
    assert snapshot(target) == before


# 4. content is checked with the program's own loaders


def test_a_settings_file_the_program_would_discard_is_rejected_naming_the_value(tmp_path):
    target = tmp_path / "t"
    make_config(target)
    before = snapshot(target)
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"trigger": "bogus-key"}'})
    with pytest.raises(BackupError, match="bogus-key") as caught:
        import_bundle(zip_path, target)
    assert caught.value.path == "settings.json" and "settings.json is not usable" in str(caught.value)
    assert snapshot(target) == before
    assert not list(tmp_path.glob("*backup*"))  # no restore point for a bundle that is refused


def test_validation_leaves_no_bak_file_and_never_writes_into_the_config_folder(tmp_path):
    target = tmp_path / "t"
    make_config(target)
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"trigger": "bogus-key"}'})
    with pytest.raises(BackupError):
        inspect_bundle(zip_path, target)
    assert sorted(p.name for p in target.iterdir()) == ["alphabets", "function_keys.json", "languages", "settings.json", "smileys"]


def test_what_the_program_itself_writes_is_accepted(tmp_path):
    src = tmp_path / "src"
    SettingsStore(src).save(Settings())  # all default fields, as the real class writes them
    keys = FunctionKeys()
    keys = keys.replace_slot(keys.slot(5).replace(macro="Hallo {Enter}", trigger="Ctrl+Alt+F5", label="Gruß"))
    FunctionKeysStore(src).save(keys)
    export_bundle(src, tmp_path / "b.zip")
    preview = inspect_bundle(tmp_path / "b.zip", tmp_path / "dst", check_shortcut=check_like_the_app)
    assert preview.warnings == ()
    assert [s.slot for s in preview.slots] == [5]


def test_an_older_settings_file_with_missing_and_unknown_keys_is_accepted(tmp_path):
    old = {"dot_threshold_ms": 220, "letter_gap_ms": 700, "trigger": "F8", "alphabet": "latin", "from_the_future": [1, 2]}
    data = json.dumps(old, indent=2).encode()
    preview = inspect_bundle(build_zip(tmp_path / "z.zip", {"settings.json": data}), tmp_path / "t")
    assert preview.warnings == ()
    target = tmp_path / "t"
    import_bundle(tmp_path / "z.zip", target)
    loaded = SettingsStore(target).load()
    assert (loaded.trigger, loaded.dot_threshold_ms) == ("F8", 220)
    assert not (target / "settings.json.bak").exists()


def test_a_file_from_before_the_shortcut_clash_check_is_accepted_with_a_note(tmp_path):
    data = b'{"trigger": "F8", "block_shortcut": "F8"}'  # the loader drops the clashing shortcut and goes on
    preview = inspect_bundle(build_zip(tmp_path / "z.zip", {"settings.json": data}), tmp_path / "t")
    assert any("kollidiert" in w for w in preview.warnings)


def test_function_keys_the_loader_would_discard_are_rejected(tmp_path):
    duplicate = b'{"schema_version": 1, "slots": [], "slots": []}'
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": duplicate})
    with pytest.raises(BackupError, match="function_keys.json is not usable") as caught:
        inspect_bundle(zip_path, tmp_path / "t")
    assert caught.value.path == "function_keys.json"


def test_a_broken_macro_is_reported_and_stays_switched_off_like_in_the_program(tmp_path):
    data = keys_file(slot(13, macro="{Loop 3}never closed"), slot(14, macro="fine"))
    preview = inspect_bundle(build_zip(tmp_path / "z.zip", {"function_keys.json": data}), tmp_path / "t")
    assert [s.slot for s in preview.slots] == [14]  # F13 is switched off by the loader, so it does not run
    assert any("F13" in w for w in preview.warnings)


def test_macros_are_parsed_with_the_check_the_caller_passes(tmp_path):
    data = keys_file(slot(13, macro="{Hyper+Q}"))
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": data})
    assert inspect_bundle(zip_path, tmp_path / "t").slots[0].slot == 13  # no check given: key names are not judged
    strict = inspect_bundle(zip_path, tmp_path / "t", check_shortcut=check_like_the_app)
    assert strict.slots == () and any("F13" in w for w in strict.warnings)


def test_function_keys_that_clash_with_the_morse_key_are_rejected_unless_left_out(tmp_path):
    target = tmp_path / "t"
    make_config(target)
    files = {"settings.json": b'{"trigger": "F13"}', "function_keys.json": keys_file(slot(13, macro="x"))}
    zip_path = build_zip(tmp_path / "z.zip", files)
    with pytest.raises(BackupError, match="clash"):
        inspect_bundle(zip_path, target)
    assert inspect_bundle(zip_path, target, exclude=["function_keys.json"]).count("replace") == 1


def test_imported_settings_are_checked_against_the_function_keys_already_there(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    (target / "function_keys.json").write_bytes(keys_file(slot(13, macro="x")))
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"trigger": "F13"}'})
    with pytest.raises(BackupError, match="clash") as caught:
        inspect_bundle(zip_path, target)
    assert caught.value.path == "settings.json"


# 6. backslashes


def backslash_zip(path: Path) -> Path:
    """zipfile turns a backslash into a slash when it writes, so the raw name is patched in afterwards."""
    data = b"{}"
    manifest = {"format": backup.FORMAT_NAME, "format_version": 1,
                "files": [{"path": "alphabets/x.json", "size": 2, "sha256": hashlib.sha256(data).hexdigest()}]}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:  # the manifest is deflated: its text has no literal match
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("alphabets/x.json", data)
    path.write_bytes(path.read_bytes().replace(b"alphabets/x.json", b"alphabets" + bytes([92]) + b"x.json"))
    return path


def test_a_backslash_in_the_stored_name_is_refused_on_every_os(tmp_path):
    zip_path = backslash_zip(tmp_path / "z.zip")
    with zipfile.ZipFile(zip_path) as z:  # what zipfile itself makes of it on this OS
        assert z.infolist()[1].orig_filename == "alphabets" + chr(92) + "x.json"
    target = tmp_path / "t"
    for call in (lambda: inspect_bundle(zip_path, target), lambda: import_bundle(zip_path, target)):
        with pytest.raises(BackupError, match="Backslashes"):
            call()
    assert not target.exists()


# 7. names


@pytest.mark.parametrize(
    "name",
    ["com¹.json", "lpt².json", "con³.json", "COM².json", "conin$.json", "conout$.json", "CLOCK$.json", "ＣＯＮ.json",
     "nul.txt.json", "ALPHAB~1.json", "a~2.json", "a\u202eb.json", "a\x7fb.json", "a\x85b.json", "a\u200bb.json",
     "a\ufeffb.json", "a\u2066b.json"],
)
def test_reserved_short_and_hiding_names_are_refused(name):
    assert not backup.is_allowed_name("alphabets/" + name)


@pytest.mark.parametrize(
    "name", ["Größe-日本語.json", "ä-test.json", "فارسی\u200cنام.json", "mein-alphabet.json", "a~b.json", "console.json", "com10.json"]
)
def test_names_existing_users_may_have_still_import(name, tmp_path):
    assert backup.is_allowed_name("alphabets/" + name)
    src = tmp_path / "src"
    (src / "alphabets").mkdir(parents=True)
    (src / "alphabets" / name).write_text('{"id": "x"}', encoding="utf-8")
    export_bundle(src, tmp_path / "b.zip")
    import_bundle(tmp_path / "b.zip", tmp_path / "dst")
    assert (tmp_path / "dst" / "alphabets" / name).read_text(encoding="utf-8") == '{"id": "x"}'


def test_display_text_writes_out_what_hides_things():
    assert backup.display_text("a\u202eb\x00c\x7fd\u200be\nf") == "a\\u202eb\\u0000c\\u007fd\\u200be\\nf"
    assert backup.display_text("Größe 日本語") == "Größe 日本語"
    assert backup.display_text("x" * 10, 4) == "xxxx…"
    entry = backup.PreviewEntry("alphabets/a\u202eb.json", "add", 1)
    assert entry.label == "alphabets/a\\u202eb.json"


# 8. restore points, TOCTOU, limits, sqlite


def make_restore_points(parent: Path, count: int, *, marker: bool = True) -> list[Path]:
    made = []
    for i in range(count):
        path = parent / f"t-backup-20260101-0000{i:02}.zip"
        with zipfile.ZipFile(path, "w") as z:
            manifest = {"format": backup.FORMAT_NAME, "format_version": 1, "files": []}
            if marker:
                manifest["restore_point"] = True
            z.writestr("manifest.json", json.dumps(manifest))
        made.append(path)
    return made


def test_only_the_newest_five_restore_points_are_kept_and_nothing_else_is_deleted(tmp_path):
    target = tmp_path / "t"
    make_config(target)
    old = make_restore_points(tmp_path, 7)
    own_export = tmp_path / "t-backup-20250101-000000.zip"  # same name pattern, but made by the user: no marker
    with zipfile.ZipFile(own_export, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": backup.FORMAT_NAME, "format_version": 1, "files": []}))
    others = [tmp_path / n for n in ("t-backup-notes.zip", "other-backup-20260101-000000.zip", "t-backup-20260101-000000.zip.bak")]
    for path in others:
        path.write_bytes(b"keep")
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"alphabet": "greek"}'})
    result = import_bundle(zip_path, target)
    survivors = {p for p in tmp_path.glob("t-backup-2026*.zip") if p.name.endswith(".zip")}
    assert survivors == {result.restore_point, *old[3:]}  # 8 restore points, the newest 5 stay
    assert not any(p.exists() for p in old[:3])
    assert own_export.exists() and all(p.read_bytes() == b"keep" for p in others)


def test_prune_counts_by_time_and_counter_and_only_deletes_marked_files(tmp_path):
    config = tmp_path / "t"
    config.mkdir()
    made = make_restore_points(tmp_path, 8)
    unmarked = made[1]  # same name pattern, but a zip of the user's own: no marker, so it is never touched
    unmarked.write_bytes(b"PK not a restore point")
    deleted = backup.prune_restore_points(config)
    assert sorted(p.name for p in deleted) == sorted(p.name for p in (made[0], made[2]))  # made[1] is the unmarked one
    assert unmarked.exists()
    assert backup.prune_restore_points(config, keep=100) == []


def test_a_restore_point_is_marked_and_a_second_in_the_same_second_sorts_after_the_first(tmp_path):
    target = tmp_path / "t"
    make_config(target)
    bundle_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"alphabet": "greek"}'})
    first = import_bundle(bundle_path, target).restore_point
    (target / "settings.json").write_text("{}")
    second = import_bundle(bundle_path, target).restore_point
    assert backup._is_restore_point(first) and backup._is_restore_point(second)
    assert first != second


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no file modes; the zip inherits the profile's access list")
def test_restore_point_and_export_are_private_to_the_user(tmp_path):
    target = tmp_path / "t"
    make_config(target)
    old_umask = os.umask(0)
    try:
        restore = import_bundle(build_zip(tmp_path / "z.zip", {"settings.json": b'{"alphabet": "greek"}'}), target).restore_point
        export_bundle(target, tmp_path / "out.zip")
    finally:
        os.umask(old_umask)
    assert (restore.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "out.zip").stat().st_mode & 0o777) == 0o600


def test_the_temp_file_is_created_fresh_even_if_an_old_one_is_in_the_way(tmp_path):
    (tmp_path / ("out.zip" + backup._TEMP_SUFFIX)).write_bytes(b"left over from a crash")
    make_config(tmp_path / "t")
    export_bundle(tmp_path / "t", tmp_path / "out.zip")
    assert zipfile.is_zipfile(tmp_path / "out.zip") and not (tmp_path / ("out.zip" + backup._TEMP_SUFFIX)).exists()


def test_the_zip_that_was_checked_is_the_zip_that_is_imported(tmp_path):
    target = tmp_path / "t"
    make_config(target)
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"alphabet": "greek"}'})
    preview = inspect_bundle(zip_path, target)
    assert preview.sha256 == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    before = snapshot(target)
    build_zip(zip_path, {"settings.json": b'{"alphabet": "cyrillic"}'})  # swapped after the check
    with pytest.raises(BackupError, match="changed after it was checked"):
        import_bundle(zip_path, target, expected_sha256=preview.sha256)
    assert snapshot(target) == before and not list(tmp_path.glob("t-backup-*"))
    fresh = inspect_bundle(zip_path, target)
    assert import_bundle(zip_path, target, expected_sha256=fresh.sha256).written == ("settings.json",)


@pytest.mark.parametrize(
    ("name", "limit"),
    [("settings.json", backup.MAX_SETTINGS_BYTES), ("function_keys.json", backup.MAX_FUNCTION_KEYS_BYTES),
     ("alphabets/a.json", backup.MAX_USER_JSON_BYTES), ("smileys/a.json", backup.MAX_USER_JSON_BYTES),
     ("languages/a.json", backup.MAX_USER_JSON_BYTES)],
)
def test_per_file_size_limits_apply_on_import_and_export(tmp_path, name, limit):
    assert (backup.MAX_SETTINGS_BYTES, backup.MAX_FUNCTION_KEYS_BYTES, backup.MAX_USER_JSON_BYTES) == (1 << 20, 8 << 20, 2 << 20)
    big = b'{"x": "' + os.urandom(limit // 2 + 1).hex().encode() + b'"}'  # random, so no zip-bomb ratio
    assert len(big) > limit
    with pytest.raises(BackupError, match="larger than") as caught:
        inspect_bundle(build_zip(tmp_path / "z.zip", {name: big}), tmp_path / "t")
    assert caught.value.path == name
    src = tmp_path / "src"
    (src / Path(name).parent).mkdir(parents=True)
    (src / name).write_bytes(big)
    with pytest.raises(BackupError, match="larger than"):
        export_bundle(src, tmp_path / "out.zip")
    assert not (tmp_path / "out.zip").exists()


def test_real_sized_files_are_far_below_the_limits(tmp_path):
    src = tmp_path / "src"
    SettingsStore(src).save(Settings())
    assert (src / "settings.json").stat().st_size < backup.MAX_SETTINGS_BYTES // 1000
    keys = FunctionKeys()
    for n in range(1, 101):  # every slot at the macro length limit
        keys = keys.replace_slot(keys.slot(n).replace(macro="é" * 10_000, trigger="", label="l" * 100))
    data = json.dumps(keys.to_dict(), indent=2, ensure_ascii=True).encode()
    assert len(data) < backup.MAX_FUNCTION_KEYS_BYTES  # even fully \u-escaped


# telecode.sqlite


def test_a_real_telecode_database_is_accepted_and_checked_on_a_copy(tmp_path, monkeypatch):
    target = tmp_path / "config-folder-x"
    make_config(target)
    opened: list[str] = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(backup.sqlite3, "connect", spy)
    zip_path = build_zip(tmp_path / "z.zip", {"telecode.sqlite": SQLITE})
    assert inspect_bundle(zip_path, target).count("add") == 1
    assert opened and all(o.endswith("?mode=ro") for o in opened)
    assert not any(target.name in o for o in opened)  # a copy in a temp folder, never the config folder
    assert not (target / "telecode.sqlite").exists()


def database_with(tmp_path: Path, sql: str) -> bytes:
    path = tmp_path / "x.sqlite"
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(sql)
    conn.commit()
    conn.close()
    return path.read_bytes()


@pytest.mark.parametrize(
    "sql",
    ["CREATE TABLE other(a);", "CREATE TABLE telecode(code TEXT);", "CREATE TABLE telecode(char TEXT, nope TEXT);"],
)
def test_a_database_without_the_telecode_table_is_rejected(tmp_path, sql):
    data = database_with(tmp_path, sql)
    with pytest.raises(BackupError, match="telecode") as caught:
        inspect_bundle(build_zip(tmp_path / "z.zip", {"telecode.sqlite": data}), tmp_path / "t")
    assert caught.value.path == "telecode.sqlite"


def test_a_damaged_or_fake_database_is_rejected(tmp_path):
    real = bytearray(SQLITE)
    fake = b"SQLite format 3\x00" + b"\x00" * 200
    cut = bytes(real[: len(real) // 2])
    for data in (fake, cut):
        with pytest.raises(BackupError, match="telecode.sqlite"):
            inspect_bundle(build_zip(tmp_path / "z.zip", {"telecode.sqlite": data}), tmp_path / "t")


def test_the_scratch_folder_is_removed_after_every_check(tmp_path, monkeypatch):
    made: list[Path] = []
    real = backup.tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real(*args, **kwargs)
        made.append(Path(path))
        return path

    monkeypatch.setattr(backup.tempfile, "mkdtemp", spy)
    ok = build_zip(tmp_path / "ok.zip", {"settings.json": b"{}", "function_keys.json": keys_file(), "telecode.sqlite": SQLITE})
    bad = build_zip(tmp_path / "bad.zip", {"settings.json": b'{"trigger": "bogus"}'})
    inspect_bundle(ok, tmp_path / "t")
    with pytest.raises(BackupError):
        inspect_bundle(bad, tmp_path / "t")
    assert len(made) >= 4 and not any(p.exists() for p in made)


# error codes and their translations


def _strings(code: str) -> dict[str, str]:
    path = Path(backup.__file__).parent / "locales" / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))["strings"]


def _placeholders(text: str) -> set[str]:
    import string

    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


@pytest.mark.parametrize("language", ["en", "de", "de-saar", "es", "fr", "it", "pt"])
def test_every_error_code_has_a_translation_with_the_same_placeholders(language):
    strings = _strings(language)
    english = _strings("en")
    for code, text in backup._MESSAGES.items():
        key = f"backup.err.{code}"
        assert key in strings, key
        want = _placeholders(text) | ({"restore_point"} if code == "write_failed_partial_point" else set())
        assert _placeholders(strings[key]) == want, key
        assert english[key] == text  # the English file says what str(error) says


def test_an_error_carries_its_code_and_parameters_and_free_text_still_works():
    error = BackupError("checksum", name="settings.json")
    assert (error.code, error.params) == ("checksum", {"name": "settings.json"})
    assert str(error) == "settings.json does not match its checksum; the zip is damaged or was changed."
    free = BackupError("some free text")
    assert free.code == "other" and str(free) == "some free text"
    point = BackupError("write_failed_partial_point", name="a", error="e", files="f", restore_point=Path("/x/r.zip"))
    assert "r.zip" in str(point) and point.restore_point == Path("/x/r.zip")


@pytest.mark.parametrize(
    ("zip_bytes", "code"),
    [(b"nope", "zip_unreadable")],
)
def test_real_failures_have_stable_codes(tmp_path, zip_bytes, code):
    path = tmp_path / "x.zip"
    path.write_bytes(zip_bytes)
    with pytest.raises(BackupError) as caught:
        inspect_bundle(path, tmp_path / "t")
    assert caught.value.code == code and caught.value.params["name"] == "x.zip"


def test_codes_of_the_main_rejections(tmp_path):
    cases = {
        "checksum": build_zip(tmp_path / "a.zip", {"settings.json": b"{}"}, manifest={
            "format": backup.FORMAT_NAME, "format_version": 1,
            "files": [{"path": "settings.json", "size": 2, "sha256": "0" * 64}]}),
        "file_unusable": build_zip(tmp_path / "b.zip", {"settings.json": b'{"trigger": "bogus-key"}'}),
        "bad_name": build_zip(tmp_path / "c.zip", {"settings.json": b"{}"}, extra={"../x.json": b"{}"}),
        "manifest_missing": build_zip(tmp_path / "d.zip", {"settings.json": b"{}"}, manifest=False),
        "backslash": backslash_zip(tmp_path / "e.zip"),
    }
    for code, zip_path in cases.items():
        with pytest.raises(BackupError) as caught:
            inspect_bundle(zip_path, tmp_path / "t")
        assert caught.value.code == code, (code, str(caught.value))


# defect pass: leftovers and existing data


def test_a_failure_that_is_no_oserror_while_writing_the_zip_leaves_no_temp_file(tmp_path, monkeypatch):
    make_config(tmp_path / "t")

    def boom(self, *_a, **_k):
        raise ValueError("zip library said no")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", boom)
    with pytest.raises(BackupError, match="zip library said no"):
        export_bundle(tmp_path / "t", tmp_path / "out.zip")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t"]


def test_the_programs_own_shipped_files_are_accepted_as_user_files(tmp_path):
    """Real data of existing users looks like what ships: alphabets, a smiley set, language files."""
    package = Path(backup.__file__).parent
    src = tmp_path / "src"
    for folder, shipped in (("alphabets", "alphabets"), ("smileys", "smiley_sets"), ("languages", "locales")):
        (src / folder).mkdir(parents=True)
        for path in (package / shipped).glob("*.json"):
            (src / folder / path.name).write_bytes(path.read_bytes())
    SettingsStore(src).save(Settings())
    manifest = export_bundle(src, tmp_path / "b.zip")
    assert len(manifest["files"]) > 14
    preview = inspect_bundle(tmp_path / "b.zip", tmp_path / "dst")
    assert preview.count("add") == len(manifest["files"]) and preview.warnings == ()
    import_bundle(tmp_path / "b.zip", tmp_path / "dst")
    assert snapshot(tmp_path / "dst") == snapshot(src)


# alphabet and smiley files are not written while an import runs


@pytest.fixture
def free_gate():
    from mourse_decoder.import_gate import gate

    gate.end()
    yield gate
    gate.end()


def test_alphabets_cannot_be_saved_renamed_or_deleted_while_an_import_runs(tmp_path, free_gate):
    from mourse_decoder.alphabet_files import (
        AlphabetError,
        delete_alphabet,
        import_alphabet_file,
        new_alphabet_text,
        save_alphabet_text,
    )
    from mourse_decoder.dictionary import DictionaryManager

    manager = DictionaryManager(user_directory=tmp_path / "alphabets")
    save_alphabet_text(manager, new_alphabet_text("eins"))  # fine while no import runs
    source = tmp_path / "zwei.json"
    source.write_text(new_alphabet_text("zwei"), encoding="utf-8")
    before = snapshot(tmp_path)
    assert free_gate.begin()
    for call in (
        lambda: save_alphabet_text(manager, new_alphabet_text("drei")),
        lambda: save_alphabet_text(manager, new_alphabet_text("eins-neu"), replacing="eins"),  # a rename
        lambda: save_alphabet_text(manager, new_alphabet_text("eins")),  # an edit
        lambda: import_alphabet_file(manager, source),
        lambda: delete_alphabet(manager, "eins"),
    ):
        with pytest.raises(AlphabetError, match="import is running"):
            call()
    assert snapshot(tmp_path) == before  # nothing was written, renamed or deleted
    free_gate.end()
    save_alphabet_text(manager, new_alphabet_text("drei"))
    delete_alphabet(manager, "eins")
    assert sorted(p.name for p in (tmp_path / "alphabets").glob("*.json")) == ["drei.json"]


def test_smiley_sets_cannot_be_imported_or_deleted_while_an_import_runs(tmp_path, free_gate):
    from mourse_decoder.smileys import SmileyError, delete_smiley_set, import_smiley_file

    builtin, user = tmp_path / "builtin", tmp_path / "user"
    builtin.mkdir()
    source = tmp_path / "s.json"
    source.write_text(json.dumps({"id": "s", "name": "S", "codes": {".-": ":)"}}), encoding="utf-8")
    import_smiley_file(source, builtin, user)
    before = snapshot(tmp_path)
    assert free_gate.begin()
    with pytest.raises(SmileyError, match="import is running"):
        import_smiley_file(source, builtin, user)
    with pytest.raises(SmileyError, match="import is running"):
        delete_smiley_set("s", user)
    assert snapshot(tmp_path) == before
    free_gate.end()
    delete_smiley_set("s", user)
    assert not (user / "s.json").exists()


def test_every_writer_of_the_config_files_asks_the_gate():
    """The only callers of the two stores' save and of apply_settings are guarded; a new caller must be added here."""
    import re

    package = Path(backup.__file__).parent
    callers = {}
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in (r"_store\.save\(", r"\.apply_settings\("):
            if re.search(pattern, text) and path.name not in ("controller.py", "settings.py", "function_keys.py"):
                callers.setdefault(path.name, []).append(pattern)
    assert sorted(callers) == ["function_keys_panel.py", "tray.py"], callers
    for name in callers:
        assert "gate." in (next(package.rglob(name))).read_text(encoding="utf-8")
