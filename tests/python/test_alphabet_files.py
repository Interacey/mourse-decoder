"""Tests for user alphabet files (create, import, edit, delete)."""

from __future__ import annotations

import json

import pytest
from mourse_decoder.alphabet_files import (
    delete_alphabet,
    import_alphabet_file,
    new_alphabet_text,
    save_alphabet_text,
)
from mourse_decoder.dictionary import AlphabetError, DictionaryManager


@pytest.fixture
def manager(tmp_path) -> DictionaryManager:
    return DictionaryManager(user_directory=tmp_path / "user")


def test_new_template_is_saveable(manager):
    info = save_alphabet_text(manager, new_alphabet_text())
    assert info.user and info.path.is_file()
    assert manager.select("mein-alphabet").lookup(".-") == "A"
    # inherits digits from common
    assert manager.select("mein-alphabet").lookup("-----") == "0"


def test_user_alphabet_is_listed_and_marked(manager):
    save_alphabet_text(manager, new_alphabet_text())
    infos = {i.id: i for i in manager.available()}
    assert infos["mein-alphabet"].user is True
    assert infos["latin"].user is False


def test_invalid_json_is_rejected_and_nothing_written(manager):
    with pytest.raises(AlphabetError, match="JSON"):
        save_alphabet_text(manager, "{ kaputt")
    assert not list((manager.user_directory).glob("*.json"))


def test_duplicate_keys_rejected(manager):
    text = '{"id": "x", "codes": {".-": "A", ".-": "B"}}'
    with pytest.raises(AlphabetError, match="doppelt"):
        save_alphabet_text(manager, text)


def test_invalid_code_rolls_back_new_file(manager):
    text = json.dumps({"id": "bad", "codes": {"abc": "A"}})
    with pytest.raises(AlphabetError, match="ungültiger Code"):
        save_alphabet_text(manager, text)
    assert not (manager.user_directory / "bad.json").exists()


def test_failed_edit_keeps_previous_content(manager):
    save_alphabet_text(manager, json.dumps({"id": "mine", "codes": {".-": "A"}}))
    before = (manager.user_directory / "mine.json").read_text(encoding="utf-8")
    with pytest.raises(AlphabetError):
        save_alphabet_text(manager, json.dumps({"id": "mine", "codes": {"x": "A"}}), replacing="mine")
    assert (manager.user_directory / "mine.json").read_text(encoding="utf-8") == before


def test_cannot_shadow_builtin(manager):
    with pytest.raises(AlphabetError, match="mitgeliefert"):
        save_alphabet_text(manager, json.dumps({"id": "latin", "codes": {".-": "A"}}))


def test_duplicate_id_rejected_for_new_but_allowed_for_edit(manager):
    text = json.dumps({"id": "mine", "codes": {".-": "A"}})
    save_alphabet_text(manager, text)
    with pytest.raises(AlphabetError, match="bereits"):
        save_alphabet_text(manager, text)
    save_alphabet_text(manager, text, replacing="mine")  # editing is fine


def test_rename_removes_old_file(manager):
    save_alphabet_text(manager, json.dumps({"id": "old", "codes": {".-": "A"}}))
    save_alphabet_text(manager, json.dumps({"id": "new", "codes": {".-": "A"}}), replacing="old")
    assert not (manager.user_directory / "old.json").exists()
    assert (manager.user_directory / "new.json").exists()


def test_extends_builtin_alphabet(manager):
    save_alphabet_text(manager, json.dumps({"id": "de-plus", "extends": "german", "codes": {"..-.-": "€"}}))
    alphabet = manager.select("de-plus")
    assert alphabet.lookup("---.") == "Ö" and alphabet.lookup("..-.-") == "€"


def test_save_invalidates_active_alphabet(manager):
    save_alphabet_text(manager, json.dumps({"id": "mine", "codes": {".-": "A"}}))
    assert manager.select("mine").lookup(".-") == "A"
    save_alphabet_text(manager, json.dumps({"id": "mine", "codes": {".-": "Z"}}), replacing="mine")
    assert manager.select("mine").lookup(".-") == "Z"


def test_import_without_id_uses_file_name(manager, tmp_path):
    src = tmp_path / "Mein Test.json"
    src.write_text(json.dumps({"name": "Importiert", "codes": {"-.-": "K"}}), encoding="utf-8")
    info = import_alphabet_file(manager, src)
    assert info.id == "mein-test"
    assert manager.select("mein-test").lookup("-.-") == "K"


def test_import_broken_file_fails_cleanly(manager, tmp_path):
    src = tmp_path / "x.json"
    src.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(AlphabetError):
        import_alphabet_file(manager, src)


def test_delete_user_alphabet(manager):
    save_alphabet_text(manager, json.dumps({"id": "mine", "codes": {".-": "A"}}))
    delete_alphabet(manager, "mine")
    assert "mine" not in {i.id for i in manager.available()}


def test_delete_builtin_refused(manager):
    with pytest.raises(AlphabetError):
        delete_alphabet(manager, "latin")


def test_delete_refused_when_extended(manager):
    save_alphabet_text(manager, json.dumps({"id": "base", "codes": {".-": "A"}}))
    save_alphabet_text(manager, json.dumps({"id": "child", "extends": "base", "codes": {"-...": "B"}}))
    with pytest.raises(AlphabetError, match="erweitert"):
        delete_alphabet(manager, "base")
