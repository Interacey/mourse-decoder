"""Tests for smiley sets and the trigger names."""

from __future__ import annotations

import json

import pytest
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager, load_alphabet
from mourse_decoder.settings import TRIGGER_NAMES, Settings, is_valid_trigger
from mourse_decoder.smileys import (
    BUILTIN_SMILEY_DIR,
    SmileyError,
    delete_smiley_set,
    import_smiley_file,
    list_smiley_sets,
    load_smiley_codes,
)
from mourse_decoder.triggers import trigger_label


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# triggers
def test_every_trigger_has_a_label():
    for name in TRIGGER_NAMES:
        assert trigger_label(name)
    assert trigger_label("MouseRight") == "Mouse 2"  # English is the default


@pytest.mark.parametrize(
    ("name", "label"),
    [("Mouse4", "Mouse 4"), ("Mouse5", "Mouse 5"), ("VK65", "A"), ("VK112", "F1"), ("VK124", "F13"),
     ("VK96", "Numpad 0"), ("VK255", "Key 255"), ("VK163", "Right Ctrl"), ("VK173", "Mute")],
)
def test_any_input_has_a_readable_label(name, label):
    assert trigger_label(name) == label


@pytest.mark.parametrize("name", ["ScrollLock", "F8", "MouseMiddle", "VK1", "VK255", "vk65", "Mouse4", "Mouse32"])
def test_valid_triggers(name):
    assert is_valid_trigger(name)


@pytest.mark.parametrize("name", ["", "VK0", "VK256", "VKx", "Mouse3", "Mouse33", "MetaRight", "Hyper", None, 5])
def test_invalid_triggers(name):
    assert not is_valid_trigger(name)


def test_settings_accept_any_key_and_extra_mouse_buttons():
    assert Settings(trigger="Mouse5").validate() == []
    assert Settings(trigger="VK65").validate() == []
    assert Settings(trigger="Mouse2").validate()

# built-in set
def test_builtin_set_is_valid_and_never_shadows_a_letter():
    codes = load_smiley_codes("standard")
    assert codes
    for alphabet_id in ("latin", "german", "greek", "cyrillic", "wabun"):
        assert not set(codes) & set(load_alphabet(alphabet_id).codes), alphabet_id


def test_list_marks_builtin_and_user(tmp_path):
    write(tmp_path / "mine.json", {"id": "mine", "name": "Meins", "codes": {"-.-.-.-.": "X"}})
    infos = {i.id: i for i in list_smiley_sets(BUILTIN_SMILEY_DIR, tmp_path)}
    assert infos["standard"].user is False
    assert infos["mine"].user is True


def test_broken_files_are_skipped_in_list(tmp_path):
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert [i.id for i in list_smiley_sets(tmp_path, None)] == []


# import
def test_import_derives_id_and_validates(tmp_path):
    src = write(tmp_path / "Meine Smileys.json", {"codes": {"-.-.-.-.": "X"}})
    info = import_smiley_file(src, BUILTIN_SMILEY_DIR, tmp_path / "user")
    assert info.id == "meine-smileys" and info.user
    assert load_smiley_codes("meine-smileys", BUILTIN_SMILEY_DIR, tmp_path / "user") == {"-.-.-.-.": "X"}


@pytest.mark.parametrize(
    "data",
    [{"codes": {"abc": "X"}}, {"codes": {".-": ""}}, {"codes": {}}, {"id": "standard", "codes": {".-.-.-.-": "X"}}],
)
def test_import_rejects_invalid(tmp_path, data):
    src = write(tmp_path / "x.json", data)
    with pytest.raises(SmileyError):
        import_smiley_file(src, BUILTIN_SMILEY_DIR, tmp_path / "user")
    assert not list((tmp_path / "user").glob("*.json")) if (tmp_path / "user").exists() else True


def test_delete_only_user_sets(tmp_path):
    write(tmp_path / "mine.json", {"id": "mine", "codes": {"-.-.-.-.": "X"}})
    delete_smiley_set("mine", tmp_path)
    with pytest.raises(SmileyError):
        delete_smiley_set("standard", tmp_path)


# manager and controller
def test_alphabet_wins_over_smiley(tmp_path):
    write(tmp_path / "s.json", {"id": "s", "codes": {".-": "😀", "--..--..": "🙂"}})
    manager = DictionaryManager(user_smiley_directory=tmp_path)
    manager.set_smileys("s")
    latin = manager.select("latin")
    assert latin.lookup(".-") == "A"
    assert latin.lookup("--..--..") == "🙂"


def test_switching_off_removes_smileys():
    manager = DictionaryManager()
    manager.set_smileys("standard")
    assert manager.select("latin").lookup("--..--..") == "🙂"
    manager.set_smileys("")
    assert manager.select("latin").lookup("--..--..") is None


def test_unknown_set_keeps_previous_state():
    manager = DictionaryManager()
    manager.set_smileys("standard")
    with pytest.raises(SmileyError):
        manager.set_smileys("gibt-es-nicht")
    assert manager.smiley_set_id == "standard"


def test_controller_types_smiley_and_applies_changes(fake_engine, injector):
    manager = DictionaryManager()
    controller = MorseController(fake_engine, injector, manager, Settings(smileys="standard"), use_hook=False)
    controller.start()
    fake_engine.letters("--..--..")
    assert injector.buffer == "🙂"

    controller.apply_settings(controller.settings.replace(smileys=""))
    fake_engine.letters("--..--..")
    assert injector.buffer == "🙂"  # nothing new typed


def test_controller_rolls_back_on_missing_set(fake_engine, injector):
    manager = DictionaryManager()
    controller = MorseController(fake_engine, injector, manager, Settings(smileys="standard"), use_hook=False)
    with pytest.raises(SmileyError):
        controller.apply_settings(controller.settings.replace(smileys="weg"))
    assert manager.smiley_set_id == "standard"
    assert controller.settings.smileys == "standard"


def test_missing_set_at_startup_does_not_crash(fake_engine, injector):
    MorseController(fake_engine, injector, DictionaryManager(), Settings(smileys="weg"), use_hook=False)
