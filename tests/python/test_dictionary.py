"""Tests for loading and validating alphabets and for lazy loading."""

from __future__ import annotations

import pytest
from mourse_decoder.dictionary import (
    BUILTIN_ALPHABET_DIR,
    AlphabetError,
    DictionaryManager,
    load_alphabet,
)

BUILTIN_IDS = ["latin", "german", "greek", "cyrillic", "wabun"]


@pytest.mark.parametrize("alphabet_id", [*BUILTIN_IDS, "common"])
def test_builtin_alphabets_load_without_errors(alphabet_id):
    # catches typos, duplicate keys and invalid codes in the JSON files
    alphabet = load_alphabet(alphabet_id)
    assert alphabet.codes


@pytest.mark.parametrize("alphabet_id", BUILTIN_IDS)
def test_builtin_alphabets_have_unique_characters(alphabet_id):
    # two codes for the same character point to a copy-paste mistake
    alphabet = load_alphabet(alphabet_id)
    values = list(alphabet.codes.values())
    duplicates = {v for v in values if values.count(v) > 1}
    assert not duplicates, f"{alphabet_id}: Zeichen mehrfach belegt: {duplicates}"


def test_builtin_files_match_their_ids():
    for path in BUILTIN_ALPHABET_DIR.glob("*.json"):
        assert load_alphabet(path.stem).id == path.stem


@pytest.mark.parametrize(
    ("alphabet_id", "code", "expected"),
    [
        ("latin", "...", "S"),
        ("latin", "---", "O"),
        ("latin", ".-", "A"),
        ("latin", "-----", "0"),  # inherited from common
        ("latin", ".--.-.", "@"),
        ("german", ".-.-", "Ä"),
        ("german", "...--..", "ß"),
        ("german", "--..", "Z"),  # inherited over two levels (german, latin)
        ("greek", "----", "Χ"),
        ("greek", ".--", "Ω"),
        ("cyrillic", ".--", "В"),
        ("cyrillic", "----", "Ш"),
        ("cyrillic", ".-.-", "Я"),
        ("wabun", "--.--", "ア"),
        ("wabun", "-", "ム"),
        ("wabun", ".-.-.-", "、"),  # overrides the full stop from common
    ],
)
def test_spot_check_known_codes(alphabet_id, code, expected):
    assert load_alphabet(alphabet_id).lookup(code) == expected


def test_latin_contains_all_letters_and_digits():
    alphabet = load_alphabet("latin")
    chars = set(alphabet.codes.values())
    assert set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") <= chars


def test_encode_is_inverse_of_lookup():
    alphabet = load_alphabet("latin")
    for code, char in alphabet.codes.items():
        assert alphabet.encode(char) == code
    assert alphabet.encode("☃") is None


def test_wabun_has_combining_marks_and_no_word_separator():
    wabun = load_alphabet("wabun")
    assert wabun.combining[".."] == "゙"  # Dakuten
    assert wabun.combining["..--."] == "゚"  # Handakuten
    assert wabun.word_separator == ""


def test_common_is_abstract():
    assert load_alphabet("common").abstract


def test_duplicate_keys_are_rejected(alphabet_dir):
    # json.loads would silently overwrite ".-"
    directory = alphabet_dir(dup='{"id": "dup", "codes": {".-": "A", ".-": "B"}}')
    with pytest.raises(AlphabetError, match="doppelter Schlüssel"):
        load_alphabet("dup", directory)


@pytest.mark.parametrize("bad_code", ["", ".x", "..._", "." * 33])
def test_invalid_codes_are_rejected(alphabet_dir, bad_code):
    directory = alphabet_dir(bad={"id": "bad", "codes": {bad_code: "A"}})
    with pytest.raises(AlphabetError, match="ungültiger Code"):
        load_alphabet("bad", directory)


def test_empty_character_is_rejected(alphabet_dir):
    directory = alphabet_dir(bad={"id": "bad", "codes": {".-": ""}})
    with pytest.raises(AlphabetError, match="nicht-leeres Zeichen"):
        load_alphabet("bad", directory)


def test_id_must_match_filename(alphabet_dir):
    directory = alphabet_dir(foo={"id": "bar", "codes": {".": "E"}})
    with pytest.raises(AlphabetError, match="Dateinamen"):
        load_alphabet("foo", directory)


def test_broken_json_reports_filename(alphabet_dir):
    directory = alphabet_dir(broken="{ das ist kein JSON")
    with pytest.raises(AlphabetError, match="broken.json"):
        load_alphabet("broken", directory)


def test_missing_alphabet(alphabet_dir):
    with pytest.raises(AlphabetError, match="nicht gefunden"):
        load_alphabet("gibtsnicht", alphabet_dir())


def test_path_traversal_ids_are_rejected(alphabet_dir):
    with pytest.raises(AlphabetError, match="ungültige Alphabet-ID"):
        load_alphabet("../etc/passwd", alphabet_dir())


def test_extends_child_overrides_parent(alphabet_dir):
    directory = alphabet_dir(
        base={"id": "base", "codes": {".": "E", "-": "T"}},
        child={"id": "child", "extends": "base", "codes": {".": "X"}},
    )
    child = load_alphabet("child", directory)
    assert child.lookup(".") == "X"  # overridden
    assert child.lookup("-") == "T"  # inherited


def test_extends_cycle_is_detected(alphabet_dir):
    directory = alphabet_dir(
        a={"id": "a", "extends": "b", "codes": {".": "A"}},
        b={"id": "b", "extends": "a", "codes": {"-": "B"}},
    )
    with pytest.raises(AlphabetError, match="zyklisch"):
        load_alphabet("a", directory)


def test_code_cannot_be_char_and_combining(alphabet_dir):
    directory = alphabet_dir(x={"id": "x", "codes": {"..": "I"}, "combining": {"..": "゙"}})
    with pytest.raises(AlphabetError, match="sowohl"):
        load_alphabet("x", directory)


def test_alphabet_without_codes_is_rejected(alphabet_dir):
    directory = alphabet_dir(empty={"id": "empty", "codes": {}})
    with pytest.raises(AlphabetError, match="keine Codes"):
        load_alphabet("empty", directory)


def test_codes_are_read_only():
    alphabet = load_alphabet("latin")
    with pytest.raises(TypeError):
        alphabet.codes["...---..."] = "SOS"  # type: ignore[index]


def test_available_lists_builtins_but_not_abstract():
    ids = {info.id for info in DictionaryManager().available()}
    assert set(BUILTIN_IDS) <= ids
    assert "common" not in ids


def test_available_skips_broken_files(alphabet_dir):
    directory = alphabet_dir(ok={"id": "ok", "name": "OK", "codes": {".": "E"}}, broken="{")
    assert [i.id for i in DictionaryManager(directory).available()] == ["ok"]


def test_available_includes_telecode_only_if_db_exists(tmp_path, telecode_db):
    without_db = DictionaryManager(telecode_db=tmp_path / "fehlt.sqlite")
    assert DictionaryManager.TELECODE_ID not in {i.id for i in without_db.available()}
    infos = {i.id: i for i in DictionaryManager(telecode_db=telecode_db).available()}
    assert infos[DictionaryManager.TELECODE_ID].kind == "telecode"


def test_nothing_is_loaded_before_select():
    manager = DictionaryManager()
    manager.available()  # reading metadata doesn't count as "loaded"
    assert manager.active is None
    assert manager.load_count == 0
    assert manager.lookup(".-") is None


def test_select_loads_lazily_and_keeps_only_one_alphabet():
    manager = DictionaryManager()
    latin = manager.select("latin")
    assert manager.lookup(".-") == "A"
    greek = manager.select("greek")
    assert manager.active is greek
    assert manager.lookup(".-") == "Α"  # Greek alpha, not Latin A
    assert manager.active is not latin
    assert manager.load_count == 2


def test_reselecting_active_alphabet_does_not_reload():
    manager = DictionaryManager()
    first = manager.select("latin")
    assert manager.select("latin") is first
    assert manager.load_count == 1


def test_select_abstract_alphabet_fails():
    with pytest.raises(AlphabetError, match="abstrakt"):
        DictionaryManager().select("common")


def test_select_telecode_via_manager_fails():
    with pytest.raises(AlphabetError, match="create_decoder"):
        DictionaryManager().select(DictionaryManager.TELECODE_ID)


def test_failed_select_keeps_previous_alphabet():
    manager = DictionaryManager()
    manager.select("latin")
    with pytest.raises(AlphabetError):
        manager.select("gibtsnicht")
    assert manager.active is not None and manager.active.id == "latin"
