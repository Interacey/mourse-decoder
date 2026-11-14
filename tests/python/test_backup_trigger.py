"""A backup import never switches the external trigger on (settings.json ``trigger_enabled``); pure logic, no display."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from mourse_decoder import backup
from mourse_decoder.backup import BackupError, export_bundle, import_bundle, inspect_bundle
from mourse_decoder.function_keys import FunctionKeysStore
from mourse_decoder.settings import SettingsStore
from test_backup import build_zip, make_config

ON = b'{"alphabet": "de", "trigger_enabled": true}'


def trigger_notes(preview) -> list:
    return [n for n in preview.notes if n.code == "trigger_off"]


def flag_notes(preview) -> list:
    return [(n.code, n.params) for n in preview.notes if n.code == "trigger_flags_removed"]


def imported_settings(tmp_path: Path, data: bytes) -> tuple[bytes, object]:
    """The settings.json an import of ``data`` leaves in an empty folder, and the preview of that import."""
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": data})
    target = tmp_path / "t" / "MourseDecoder"
    preview = inspect_bundle(zip_path, target)
    import_bundle(zip_path, target)
    return (target / "settings.json").read_bytes(), preview


def keys_file(*slots: dict) -> bytes:
    return json.dumps({"schema_version": 1, "slots": list(slots)}).encode()


def imported_keys(tmp_path: Path, data: bytes, **kwargs) -> tuple[bytes, object]:
    """The function_keys.json an import of ``data`` leaves in an empty folder, and the preview of that import."""
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": data})
    target = tmp_path / "t" / "MourseDecoder"
    preview = inspect_bundle(zip_path, target, **kwargs)
    import_bundle(zip_path, target, **kwargs)
    return (target / "function_keys.json").read_bytes(), preview


def slot(number: int, **fields) -> dict:
    return {"slot": number, "trigger": f"F{number}", "macro": "hello", "enabled": True, "swallow": True, **fields}


def zip_member(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


def loaded_trigger(target: Path) -> object:
    """``trigger_enabled`` of the settings the program loads from ``target`` (False while Settings has no such field)."""
    return getattr(SettingsStore(target).load(), "trigger_enabled", False)


# settings.json


def test_an_enabled_trigger_in_a_bundle_is_written_as_false_with_a_note(tmp_path):
    written, preview = imported_settings(tmp_path, ON)
    assert json.loads(written) == {"alphabet": "de", "trigger_enabled": False}
    assert written == json.dumps({"alphabet": "de", "trigger_enabled": False}, indent=2, ensure_ascii=False).encode()
    assert [(n.code, n.params) for n in trigger_notes(preview)] == [("trigger_off", {})]


def test_the_rewrite_keeps_key_order_and_other_values(tmp_path):
    data = '{"alphabet": "de", "label": "größe ✓", "trigger_enabled": true, "typing_delay_ms": 5, "x": {"a": [1, 2]}}'.encode()
    written, _ = imported_settings(tmp_path, data)
    assert list(json.loads(written)) == ["alphabet", "label", "trigger_enabled", "typing_delay_ms", "x"]
    assert json.loads(written) == {**json.loads(data), "trigger_enabled": False}
    assert "größe ✓".encode() in written  # no \u escapes, like SettingsStore.save


def test_a_lenient_file_with_a_raw_line_break_is_neutralised_too(tmp_path):
    data = b'{"alphabet": "de", "note": "a\nb", "trigger_enabled": true}'  # accepted by the settings loader
    written, preview = imported_settings(tmp_path, data)
    assert json.loads(written)["trigger_enabled"] is False
    assert trigger_notes(preview)


@pytest.mark.parametrize(
    "data",
    [
        b'{"alphabet": "de"}',
        b'{"alphabet": "de", "trigger_enabled": false}',
        b'\xef\xbb\xbf{ "alphabet":"de" ,\n"trigger_enabled" :false}',
        b'{"alphabet": "de", "trigger_enabled": true, "trigger_enabled": false}',  # the last one counts
    ],
)
def test_a_file_without_an_enabled_trigger_stays_byte_for_byte(tmp_path, data):
    written, preview = imported_settings(tmp_path, data)
    assert written == data
    assert not trigger_notes(preview)


@pytest.mark.parametrize("key", ["Trigger_Enabled", "TRIGGER_ENABLED", "trigger-enabled", "trigger_enabled "])
def test_another_spelling_of_the_key_is_not_read_by_the_program_and_stays(tmp_path, key):
    data = f'{{"alphabet": "de", "{key}": true}}'.encode()
    written, preview = imported_settings(tmp_path, data)
    assert written == data and not trigger_notes(preview)
    assert loaded_trigger(tmp_path / "t" / "MourseDecoder") is False


@pytest.mark.parametrize(
    "data",
    [
        b'{"trigger_enabled": false, "trigger_enabled": true}',
        b'{"trigger_enabled": true, "alphabet": "de", "trigger_enabled": true}',
        b'{"trigger\\u005fenabled": true}',  # the escaped spelling is the same key for the parser
        b'{"trigger_enabled": 0, "trigger_enabled": true}',
    ],
)
def test_a_repeated_or_escaped_key_cannot_smuggle_the_trigger_in(tmp_path, data):
    written, preview = imported_settings(tmp_path, data)
    assert json.loads(written)["trigger_enabled"] is False
    assert written.count(b"trigger_enabled") == 1
    assert trigger_notes(preview)
    assert loaded_trigger(tmp_path / "t" / "MourseDecoder") is False


@pytest.mark.parametrize("value", ['"true"', "1", "null", "[true]", '"yes"'])
def test_a_value_that_is_no_boolean_is_not_rewritten_and_never_enables_the_trigger(tmp_path, value):
    data = f'{{"alphabet": "de", "trigger_enabled": {value}}}'.encode()
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": data})
    target = tmp_path / "t" / "MourseDecoder"
    try:
        preview = inspect_bundle(zip_path, target)
        import_bundle(zip_path, target)
    except BackupError as exc:  # the settings validation refuses the file: nothing is written
        assert exc.code == "file_unusable" and not target.exists()
        return
    assert (target / "settings.json").read_bytes() == data and not trigger_notes(preview)
    assert not loaded_trigger(target)


def test_import_without_macros_still_turns_the_trigger_off(tmp_path):
    files = {"settings.json": ON, "function_keys.json": keys_file(slot(13, triggerable=True))}
    zip_path = build_zip(tmp_path / "z.zip", files)
    target = tmp_path / "t" / "MourseDecoder"
    preview = inspect_bundle(zip_path, target)
    assert preview.safe is not None and trigger_notes(preview.safe)
    import_bundle(zip_path, target, exclude=preview.risky_files)
    assert not (target / "function_keys.json").exists()
    assert json.loads((target / "settings.json").read_bytes())["trigger_enabled"] is False


def test_the_zip_check_still_matches_after_the_rewrite(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": ON})
    target = tmp_path / "t" / "MourseDecoder"
    preview = inspect_bundle(zip_path, target)
    import_bundle(zip_path, target, expected_sha256=preview.sha256)  # the digest is of the zip, not of the rewrite
    assert json.loads((target / "settings.json").read_bytes())["trigger_enabled"] is False


# restore points


def own_state(tmp_path: Path) -> Path:
    """A config folder where the user has the trigger on."""
    cfg = make_config(tmp_path / "cfg" / "MourseDecoder")
    (cfg / "settings.json").write_bytes(ON)
    (cfg / "function_keys.json").write_bytes(keys_file(slot(13, triggerable=True)))
    return cfg


def test_a_restore_point_of_this_folder_brings_the_users_own_trigger_setting_back(tmp_path):
    cfg = own_state(tmp_path)
    before = {name: (cfg / name).read_bytes() for name in ("settings.json", "function_keys.json")}
    foreign = build_zip(tmp_path / "foreign.zip", {"settings.json": b'{"alphabet": "greek"}'})
    result = import_bundle(foreign, cfg)
    assert (cfg / "settings.json").read_bytes() == b'{"alphabet": "greek"}'
    assert zip_member(result.restore_point, "settings.json") == ON  # the user's state is in the restore point

    assert not trigger_notes(inspect_bundle(result.restore_point, cfg))
    assert not flag_notes(inspect_bundle(result.restore_point, cfg))
    import_bundle(result.restore_point, cfg)
    assert {name: (cfg / name).read_bytes() for name in before} == before


def test_a_restore_point_of_this_folder_brings_the_users_trigger_flags_back(tmp_path):
    cfg = own_state(tmp_path)
    before = (cfg / "function_keys.json").read_bytes()
    foreign = build_zip(tmp_path / "foreign.zip", {"function_keys.json": keys_file(slot(14, triggerable=True))})
    result = import_bundle(foreign, cfg)
    assert b"triggerable" not in (cfg / "function_keys.json").read_bytes()
    assert zip_member(result.restore_point, "function_keys.json") == before
    assert not flag_notes(inspect_bundle(result.restore_point, cfg))
    import_bundle(result.restore_point, cfg)
    assert (cfg / "function_keys.json").read_bytes() == before
    assert b'"triggerable": true' in before


def test_the_same_zip_from_anywhere_else_is_foreign_and_neutralised(tmp_path):
    cfg = own_state(tmp_path)
    foreign = build_zip(tmp_path / "foreign.zip", {"settings.json": b'{"alphabet": "greek"}'})
    point = import_bundle(foreign, cfg).restore_point
    elsewhere = tmp_path / "downloads"
    elsewhere.mkdir()
    copy = elsewhere / point.name  # same name, other folder
    shutil.copy(point, copy)
    renamed = point.with_name("renamed.zip")  # same folder, a name that no restore point has
    shutil.copy(point, renamed)
    for candidate in (copy, renamed):
        assert trigger_notes(inspect_bundle(candidate, cfg)), candidate
        import_bundle(candidate, cfg)
        assert json.loads((cfg / "settings.json").read_bytes())["trigger_enabled"] is False, candidate
        (cfg / "settings.json").write_bytes(b'{"alphabet": "greek"}')


def test_a_zip_that_only_claims_to_be_a_restore_point_is_neutralised(tmp_path):
    cfg = make_config(tmp_path / "cfg" / "MourseDecoder")
    manifest = {
        "format": backup.FORMAT_NAME, "format_version": backup.FORMAT_VERSION, "restore_point": True,
        "restore_created": [], "files": [{"path": "settings.json", "size": len(ON), "sha256": backup._sha256(ON)}],
    }
    zip_path = build_zip(tmp_path / "x.zip", {"settings.json": ON}, manifest=manifest)
    assert trigger_notes(inspect_bundle(zip_path, cfg))
    import_bundle(zip_path, cfg)
    assert json.loads((cfg / "settings.json").read_bytes())["trigger_enabled"] is False


# summary of the slots


def test_a_triggerable_slot_gets_the_external_trigger_label(tmp_path):
    data = keys_file(
        slot(13, triggerable=True),
        slot(14),
        slot(15, triggerable=False),
        slot(16, triggerable=True, enabled=False),  # never runs: not listed
    )
    preview = inspect_bundle(build_zip(tmp_path / "z.zip", {"function_keys.json": data}), tmp_path / "t")
    by_slot = {s.slot: s for s in preview.slots}
    assert sorted(by_slot) == [13, 14, 15]
    assert by_slot[13].risks == (backup.EXTERNAL_TRIGGER_RISK,) == ("external trigger",)
    assert by_slot[14].risks == () and by_slot[15].risks == ()
    assert preview.risky and preview.risky_files == ("function_keys.json",)
    assert preview.safe is not None and preview.safe.slots == ()


def test_no_triggerable_slot_means_no_label_and_no_risk(tmp_path):
    data = keys_file(slot(13), slot(14, trigger="Ctrl+F5"))
    preview = inspect_bundle(build_zip(tmp_path / "z.zip", {"function_keys.json": data}), tmp_path / "t")
    assert all(s.risks == () for s in preview.slots) and not preview.risky


# function_keys.json


def test_a_foreign_bundle_loses_the_trigger_flags_of_its_slots_and_says_so(tmp_path):
    data = keys_file(slot(13, triggerable=True, macro="{Win+R}calc{Enter}", label="Rechner ✓"), slot(14, label="x"))
    written, preview = imported_keys(tmp_path, data)
    slots = [slot(13, macro="{Win+R}calc{Enter}", label="Rechner ✓"), slot(14, label="x")]
    expected = {"schema_version": 1, "slots": slots}
    assert json.loads(written) == expected
    assert written == json.dumps(expected, indent=2, ensure_ascii=False).encode()  # the way the store writes it
    assert flag_notes(preview) == [("trigger_flags_removed", {})]
    keys, _ = FunctionKeysStore(tmp_path / "t" / "MourseDecoder").load()
    assert not any(s.triggerable for s in keys.slots)
    assert keys.slot(13).macro == "{Win+R}calc{Enter}" and keys.slot(13).enabled


def test_the_confirmation_still_shows_what_the_zip_contains(tmp_path):
    data = keys_file(slot(13, triggerable=True))
    preview = inspect_bundle(build_zip(tmp_path / "z.zip", {"function_keys.json": data}), tmp_path / "t")
    assert preview.slots[0].risks == (backup.EXTERNAL_TRIGGER_RISK,)
    assert flag_notes(preview)
    assert preview.safe is not None and not flag_notes(preview.safe)  # without the macros there is nothing to remove


BOM = b"\xef\xbb\xbf"


@pytest.mark.parametrize(
    "data",
    [
        keys_file(slot(13), slot(14, trigger="Ctrl+F5")),
        keys_file(slot(13, triggerable=False)),
        keys_file(),
        BOM + b'{ "schema_version":1 ,\n"slots" :[{"slot":13,"trigger":"F13","macro":"a","triggerable":false}]}',
        b'{"schema_version": 1, "slots": [{"slot": 13, "trigger": "F13", "macro": "a", "triggerable": 0}]}',
    ],
)
def test_a_file_without_a_set_flag_stays_byte_for_byte(tmp_path, data):
    written, preview = imported_keys(tmp_path, data)
    assert written == data
    assert not flag_notes(preview)


def test_mixed_slots_lose_only_the_flag(tmp_path):
    data = keys_file(
        slot(13, triggerable=True), slot(14), slot(15, triggerable=False), slot(16, triggerable=True, enabled=False),
        slot(17, typing_delay_ms=30, triggerable=True),
    )
    written, preview = imported_keys(tmp_path, data)
    assert json.loads(written)["slots"] == [
        slot(13), slot(14), slot(15), slot(16, enabled=False), slot(17, typing_delay_ms=30)
    ]
    assert len(flag_notes(preview)) == 1


@pytest.mark.parametrize("value", ['"yes"', "1", "[true]", '"false"'])
def test_a_flag_with_a_value_that_is_no_boolean_is_removed_too(tmp_path, value):
    entry = f'{{"slot": 13, "trigger": "F13", "macro": "a", "triggerable": {value}}}'
    written, preview = imported_keys(tmp_path, f'{{"schema_version": 1, "slots": [{entry}]}}'.encode())
    assert b"triggerable" not in written
    assert flag_notes(preview)


def test_a_byte_order_mark_does_not_hide_the_flags(tmp_path):
    written, preview = imported_keys(tmp_path, BOM + keys_file(slot(13, triggerable=True)))
    assert b"triggerable" not in written and flag_notes(preview)
    assert not written.startswith(BOM)


def test_two_entries_for_one_slot_lose_the_flag_in_both(tmp_path):
    data = keys_file(slot(13, macro="a"), slot(13, macro="b", triggerable=True))  # the loader reads the first one
    written, preview = imported_keys(tmp_path, data)
    assert b"triggerable" not in written and flag_notes(preview)


def test_a_repeated_triggerable_key_is_refused_by_the_check_and_nothing_is_written(tmp_path):
    data = b'{"slots": [{"slot": 13, "trigger": "F13", "macro": "a", "triggerable": true, "triggerable": false}]}'
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": data})
    target = tmp_path / "t" / "MourseDecoder"
    with pytest.raises(BackupError) as caught:
        import_bundle(zip_path, target)
    assert caught.value.code == "file_unusable" and not target.exists()


def test_without_settings_json_the_flags_go_while_the_trigger_is_on_here(tmp_path):
    cfg = own_state(tmp_path)
    data = keys_file(slot(14, macro="{Win+R}calc{Enter}", triggerable=True))
    zip_path = build_zip(tmp_path / "foreign.zip", {"function_keys.json": data})
    preview = inspect_bundle(zip_path, cfg)
    assert flag_notes(preview) and not trigger_notes(preview)  # there is no settings.json in the zip to switch off
    import_bundle(zip_path, cfg)
    assert (cfg / "settings.json").read_bytes() == ON  # the user's own switch is not touched
    keys, _ = FunctionKeysStore(cfg).load()
    assert keys.slot(14).macro == "{Win+R}calc{Enter}" and not any(s.triggerable for s in keys.slots)


def test_with_settings_json_both_notes_come(tmp_path):
    files = {"settings.json": ON, "function_keys.json": keys_file(slot(13, triggerable=True))}
    preview = inspect_bundle(build_zip(tmp_path / "z.zip", files), tmp_path / "t")
    assert trigger_notes(preview) and flag_notes(preview)


def test_without_the_macros_there_is_no_flag_note_and_no_file(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": keys_file(slot(13, triggerable=True))})
    target = tmp_path / "t" / "MourseDecoder"
    preview = inspect_bundle(zip_path, target, exclude=["function_keys.json"])
    assert not flag_notes(preview)
    import_bundle(zip_path, target, exclude=["function_keys.json"])
    assert not (target / "function_keys.json").exists()


def test_the_zip_check_matches_after_the_flags_are_removed(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": keys_file(slot(13, triggerable=True))})
    target = tmp_path / "t" / "MourseDecoder"
    preview = inspect_bundle(zip_path, target)
    import_bundle(zip_path, target, expected_sha256=preview.sha256)
    assert b"triggerable" not in (target / "function_keys.json").read_bytes()


# export and import


def test_round_trip_into_another_folder_drops_the_trigger_flags_and_the_trigger_switch(tmp_path):
    cfg = own_state(tmp_path)
    zip_path = tmp_path / "b.zip"
    export_bundle(cfg, zip_path)
    assert zip_member(zip_path, "settings.json") == ON  # the export copies the file as it is
    target = tmp_path / "new" / "MourseDecoder"
    import_bundle(zip_path, target)
    assert b'"triggerable": true' in (cfg / "function_keys.json").read_bytes()
    assert zip_member(zip_path, "function_keys.json") == (cfg / "function_keys.json").read_bytes()
    assert json.loads((target / "function_keys.json").read_bytes()) == {"schema_version": 1, "slots": [slot(13)]}
    assert json.loads((target / "settings.json").read_bytes()) == {"alphabet": "de", "trigger_enabled": False}
    for name in ("alphabets/mein.json", "smileys/set.json", "languages/xx.json"):
        assert (target / name).read_bytes() == (cfg / name).read_bytes()


def test_the_backup_whitelist_has_no_trigger_log_and_the_key_fields_are_unchanged():
    assert not backup.is_allowed_name("trigger.log")
    assert "trigger_enabled" not in backup.KEY_FIELDS
