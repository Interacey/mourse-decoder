"""Review round C: settings, alphabets, smileys, telecode, i18n, function keys, triggers."""

from __future__ import annotations

import json
import sqlite3

import pytest
from mourse_decoder import i18n, jsontext
from mourse_decoder.alphabet_files import read_alphabet_text, save_alphabet_text
from mourse_decoder.decoders import TelecodeDecoder, create_decoder
from mourse_decoder.dictionary import AlphabetError, DictionaryManager
from mourse_decoder.function_keys import FunctionKeys, FunctionKeysStore, Slot
from mourse_decoder.settings import Settings, SettingsStore, is_valid_binding, is_valid_trigger
from mourse_decoder.smileys import import_smiley_file
from mourse_decoder.telecode import TelecodeStore, build_database


def alphabet(alphabet_id: str, **extra) -> str:
    return json.dumps({"id": alphabet_id, "codes": {".-": "A"}, **extra})


@pytest.fixture
def manager(tmp_path) -> DictionaryManager:
    return DictionaryManager(user_directory=tmp_path / "user")


# 1. rename keeps dependants working

def test_rename_is_refused_while_another_alphabet_extends_it(manager):
    save_alphabet_text(manager, alphabet("parent"))
    save_alphabet_text(manager, alphabet("child", extends="parent"))
    with pytest.raises(AlphabetError, match="umbenannt"):
        save_alphabet_text(manager, alphabet("parent2"), replacing="parent")
    assert manager.user_directory.joinpath("parent.json").is_file()
    assert not manager.user_directory.joinpath("parent2.json").exists()
    assert manager.select("child").lookup(".-") == "A"


def test_missing_parent_error_names_the_user_folder(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "orphan.json").write_text(alphabet("orphan", extends="gone"), encoding="utf-8")
    with pytest.raises(AlphabetError) as info:
        DictionaryManager(user_directory=user).select("orphan")
    assert str(user) in str(info.value)


# 2. atomic writes

def test_alphabet_save_leaves_no_temp_file_and_rolls_back_atomically(manager):
    save_alphabet_text(manager, alphabet("mine"))
    with pytest.raises(AlphabetError):
        save_alphabet_text(manager, alphabet("mine", extends="nope"), replacing="mine")
    names = sorted(p.name for p in manager.user_directory.iterdir())
    assert names == ["mine.json"]
    assert json.loads((manager.user_directory / "mine.json").read_text("utf-8"))["codes"] == {".-": "A"}


def test_write_atomic_replaces_via_temp_file(tmp_path):
    target = tmp_path / "x.json"
    jsontext.write_atomic(target, "a")
    jsontext.write_atomic(target, b"b")
    assert target.read_text() == "b"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_smiley_import_leaves_no_temp_file(tmp_path):
    source = tmp_path / "s.json"
    source.write_text(json.dumps({"id": "mine", "codes": {"..--": "x"}}), encoding="utf-8")
    import_smiley_file(source, tmp_path / "builtin", tmp_path / "user")
    assert [p.name for p in (tmp_path / "user").iterdir()] == ["mine.json"]


# 3. BOM

def test_alphabet_with_bom_loads_and_reads(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    path = user / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + alphabet("bom").encode("utf-8"))
    manager = DictionaryManager(user_directory=user)
    assert "bom" in [i.id for i in manager.available()]
    assert manager.select("bom").lookup(".-") == "A"
    info = next(i for i in manager.available() if i.id == "bom")
    assert not read_alphabet_text(info).startswith("﻿")


def test_alphabet_with_stray_quote_loads(tmp_path):
    (tmp_path / "q.json").write_text('{"id": "q", "name": "Say "hi" now", "codes": {".-": "A"}}', encoding="utf-8")
    assert DictionaryManager(directory=tmp_path).select("q").name == 'Say "hi" now'


def test_alphabet_duplicate_keys_still_rejected(tmp_path):
    (tmp_path / "d.json").write_text('{"id": "d", "codes": {".-": "A", ".-": "B"}}', encoding="utf-8")
    with pytest.raises(AlphabetError, match="doppelt"):
        DictionaryManager(directory=tmp_path).select("d")


def test_settings_with_bom_are_kept(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"alphabet": "wabun", "theme": "dark"}).encode("utf-8"))
    loaded = store.load()
    assert (loaded.alphabet, loaded.theme) == ("wabun", "dark")
    assert not store.path.with_suffix(".json.bak").exists()


# 4. trailing newline

@pytest.mark.parametrize("bad", ["abc\n", "-abc", "ABC", ""])
def test_alphabet_id_with_trailing_newline_is_rejected(manager, bad):
    with pytest.raises(AlphabetError):
        save_alphabet_text(manager, json.dumps({"id": bad, "codes": {".-": "A"}}))


def test_code_with_trailing_newline_is_rejected(tmp_path):
    (tmp_path / "c.json").write_text(json.dumps({"id": "c", "codes": {".-\n": "A"}}), encoding="utf-8")
    with pytest.raises(AlphabetError, match="ungültiger Code"):
        DictionaryManager(directory=tmp_path).select("c")


def test_settings_cycle_id_with_trailing_newline_is_rejected():
    assert Settings(cycle_alphabets=("abc\n",)).validate()


def test_smiley_id_and_code_with_trailing_newline_are_rejected(tmp_path):
    source = tmp_path / "s.json"
    source.write_text(json.dumps({"id": "abc\n", "codes": {"..--": "x"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="ungültig"):
        import_smiley_file(source, tmp_path / "b", tmp_path / "u")
    source.write_text(json.dumps({"id": "abc", "codes": {"..--\n": "x"}}), encoding="utf-8")
    with pytest.raises(ValueError):
        import_smiley_file(source, tmp_path / "b", tmp_path / "u")


def test_telecode_lookup_is_ascii_digits_without_newline(tmp_path):
    db = tmp_path / "t.db"
    build_database([("0022", "中")], db)
    with TelecodeStore(db) as store:
        assert store.lookup("0022") == "中"
        assert store.lookup("0022\n") is None
        assert store.lookup("٠٠٢٢") is None


# 5. ASCII digits in bindings

@pytest.mark.parametrize("spec", ["VK١٢٣", "Mouse٥", "Ctrl+VK١٢"])
def test_binding_with_unicode_digits_is_invalid(spec):
    assert not is_valid_binding(spec)


@pytest.mark.parametrize("name", ["VK12\n", "Mouse5\n", "F13\n", "VK١٢"])
def test_trigger_name_with_newline_or_unicode_digits_is_invalid(name):
    assert not is_valid_trigger(name)


# 6. shortcuts versus Morse keys

def test_defaults_validate():
    assert Settings().validate() == []


def test_default_dash_key_clashes_with_default_shortcut():
    errors = Settings(separate_dash_key=True).validate()
    assert any("stören sich" in e and "Pause" in e for e in errors)
    assert Settings(separate_dash_key=True, block_shortcut="").validate() == []


def test_dash_key_ignored_while_not_separate():
    assert Settings(dash_trigger="Pause", separate_dash_key=False).validate() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"trigger": "Pause"},                                    # key fires under the shortcut's modifiers
        {"trigger": "Ctrl+Shift+Pause"},                         # same key as the shortcut
        {"trigger": "Shift+Pause"},
        {"cycle_shortcut": "Ctrl+F8", "trigger": "F8"},          # dot key shadows the shortcut
        {"separate_dash_key": True, "trigger": "F8", "dash_trigger": "Ctrl+F8"},
        {"separate_dash_key": True, "trigger": "Ctrl+F8", "dash_trigger": "F8"},
    ],
)
def test_clashing_keys_are_refused(changes):
    assert Settings(**changes).validate()


@pytest.mark.parametrize(
    "changes",
    [
        {"trigger": "F8", "cycle_shortcut": "Ctrl+F9"},
        {"trigger": "Ctrl+F8", "block_shortcut": "Ctrl+Shift+F9"},
        {"separate_dash_key": True, "trigger": "F8", "dash_trigger": "F9", "block_shortcut": ""},
        {"trigger": "CapsLock+VK81", "block_shortcut": "Ctrl+Shift+Pause"},
    ],
)
def test_distinct_keys_are_fine(changes):
    assert Settings(**changes).validate() == []


# 7. disabled slots do not block the table

def keys_with(*slots: Slot) -> FunctionKeys:
    keys = FunctionKeys()
    for slot in slots:
        keys = keys.replace_slot(slot)
    return keys


def test_disabled_slot_with_bad_trigger_validates():
    keys = keys_with(Slot(14, trigger="Ctrl+Foo", macro="x", enabled=False))
    assert keys.validate() == []
    enabled = keys_with(Slot(14, trigger="Ctrl+Foo", macro="x", enabled=True))
    assert enabled.validate() == ["F14: Ungültiger Auslöser 'Ctrl+Foo'."]


def test_file_with_bad_disabled_slot_loads_and_saves(tmp_path):
    store = FunctionKeysStore(tmp_path)
    store.path.write_text(
        json.dumps({"slots": [{"slot": 14, "trigger": "Ctrl+Foo", "macro": "x", "enabled": False}]}), encoding="utf-8"
    )
    keys, _ = store.load()
    assert keys.validate() == []
    store.save(keys.replace_slot(keys.slot(13).replace(macro="hi")))


def test_enabled_slot_with_bad_trigger_is_switched_off_and_table_still_validates(tmp_path):
    store = FunctionKeysStore(tmp_path)
    store.path.write_text(
        json.dumps({"slots": [{"slot": 14, "trigger": "Ctrl+Foo", "macro": "x"}, {"slot": 13, "macro": "y"}]}),
        encoding="utf-8",
    )
    keys, warnings = store.load()
    assert warnings and keys.slot(14).enabled is False
    assert keys.validate() == []
    assert keys.slot(13).active


# 8. Space as VK32

@pytest.mark.parametrize("trigger", ["VK32", "Shift+VK32"])
def test_vk32_is_typing_like_space(trigger):
    errors = keys_with(Slot(5, trigger=trigger, macro="x")).validate()
    assert errors and "normales Tippen" in errors[0]


def test_vk32_with_ctrl_is_allowed():
    assert keys_with(Slot(5, trigger="Ctrl+VK32", macro="x")).validate() == []


# 9. Esc as Morse key

@pytest.mark.parametrize("trigger", ["VK27", "Ctrl+VK27", "strg+vk027"])
def test_esc_is_no_morse_key(trigger):
    assert any("Esc" in e for e in Settings(trigger=trigger).validate())
    assert any("Esc" in e for e in Settings(separate_dash_key=True, dash_trigger=trigger, block_shortcut="").validate())


def test_esc_as_unused_dash_key_does_not_matter():
    assert Settings(dash_trigger="VK27", separate_dash_key=False).validate() == []


# 10. i18n format specs

@pytest.fixture
def user_language(tmp_path):
    def make(text: str) -> None:
        (tmp_path / "xx.json").write_text(json.dumps({"id": "xx", "name": "X", "strings": {"k": text}}), encoding="utf-8")
        i18n.set_language("xx", tmp_path)

    yield make
    i18n._user_dir = None  # set_language keeps the last user folder otherwise
    i18n.set_language("en")


@pytest.mark.parametrize("text", ["{n:>999999999}", "{n:>40}", "{n!r}", "{n:{n}}", "{n!s:>3}"])
def test_placeholders_with_spec_or_conversion_return_raw_text(user_language, text):
    user_language(text)
    assert i18n.tr("k", n=5) == text


def test_plain_placeholders_still_work(user_language):
    user_language("a {n} b {m}")
    assert i18n.tr("k", n=5, m="x") == "a 5 b x"


# 11. .bak rotation

def test_settings_backup_keeps_the_oldest_file(tmp_path):
    store = SettingsStore(tmp_path)
    store.path.write_text("{first", encoding="utf-8")
    store.load()
    store.path.write_text("{second", encoding="utf-8")
    store.load()
    assert store.path.with_suffix(".json.bak").read_text() == "{first"
    assert tmp_path.joinpath("settings.json.bak2").read_text() == "{second"


def test_function_keys_backup_keeps_the_oldest_file(tmp_path):
    store = FunctionKeysStore(tmp_path)
    store.path.write_text("{first", encoding="utf-8")
    store.load()
    store.path.write_text(json.dumps({"slots": [{"slot": 14, "macro": "x", "swallow": 3}, 5]}), encoding="utf-8")
    store.load()
    assert tmp_path.joinpath("function_keys.json.bak").read_text() == "{first"
    assert "swallow" in tmp_path.joinpath("function_keys.json.bak2").read_text()


def test_same_content_reuses_the_backup(tmp_path):
    store = FunctionKeysStore(tmp_path)
    store.path.write_text(json.dumps({"slots": [5]}), encoding="utf-8")
    store.load()
    store.load()
    assert sorted(p.name for p in tmp_path.glob("*.bak*")) == ["function_keys.json.bak"]


def test_backup_count_is_capped(tmp_path):
    path = tmp_path / "f.json"
    for n in range(12):
        path.write_text(str(n), encoding="utf-8")
        jsontext.backup_file(path, move=False)
    assert len(list(tmp_path.glob("f.json.bak*"))) == jsontext.MAX_BACKUPS
    assert (tmp_path / "f.json.bak").read_text() == "0"


# 13. decoder close

def test_telecode_decoder_close_closes_the_connection(tmp_path):
    db = tmp_path / "t.db"
    build_database([("0022", "中")], db)
    manager = DictionaryManager(telecode_db=db)
    decoder = create_decoder(manager, DictionaryManager.TELECODE_ID)
    assert isinstance(decoder, TelecodeDecoder)
    decoder.close()
    with pytest.raises(sqlite3.ProgrammingError):
        decoder._store.lookup("0022")


def test_alphabet_decoder_close_is_a_no_op():
    create_decoder(DictionaryManager(), "latin").close()
