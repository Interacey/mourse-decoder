"""Tests for AlphabetDecoder (dakuten included) and TelecodeDecoder."""

from __future__ import annotations

import pytest
from mourse_decoder.decoders import AlphabetDecoder, Emission, TelecodeDecoder, create_decoder
from mourse_decoder.dictionary import AlphabetError, DictionaryManager, load_alphabet
from mourse_decoder.telecode import TelecodeStore

DAKUTEN = ".."
HANDAKUTEN = "..--."


def test_simple_lookup():
    decoder = AlphabetDecoder(load_alphabet("latin"))
    assert decoder.decode("...") == Emission("S")
    assert decoder.decode("---") == Emission("O")


def test_unknown_sequence_returns_none():
    decoder = AlphabetDecoder(load_alphabet("latin"))
    assert decoder.decode("........") is None


def test_word_gap_emits_separator():
    assert AlphabetDecoder(load_alphabet("latin")).word_gap() == Emission(" ")


def test_word_gap_without_separator_emits_nothing():
    assert AlphabetDecoder(load_alphabet("wabun")).word_gap() is None


@pytest.mark.parametrize(
    ("base_code", "mark", "expected"),
    [
        (".-..", DAKUTEN, "ガ"),  # カ + ゛
        ("-.-.-", DAKUTEN, "ザ"),  # サ + ゛
        ("..-", DAKUTEN, "ヴ"),  # ウ + ゛ (special case, exists as its own codepoint)
        ("-...", HANDAKUTEN, "パ"),  # ハ + ゜
        ("--..", HANDAKUTEN, "プ"),  # フ + ゜
    ],
)
def test_wabun_combining_marks(base_code, mark, expected):
    decoder = AlphabetDecoder(load_alphabet("wabun"))
    decoder.decode(base_code)
    # delete one character (the base kana) and type the composed one
    assert decoder.decode(mark) == Emission(expected, erase=1)


def test_dakuten_without_previous_kana_is_ignored():
    decoder = AlphabetDecoder(load_alphabet("wabun"))
    assert decoder.decode(DAKUTEN) is None


def test_invalid_combination_is_ignored():
    # ア can't take a dakuten, so type nothing rather than a loose "゛"
    decoder = AlphabetDecoder(load_alphabet("wabun"))
    decoder.decode("--.--")
    assert decoder.decode(DAKUTEN) is None


def test_double_dakuten_is_ignored():
    decoder = AlphabetDecoder(load_alphabet("wabun"))
    decoder.decode(".-..")
    decoder.decode(DAKUTEN)  # ガ
    assert decoder.decode(DAKUTEN) is None


def test_no_combining_across_word_gap():
    decoder = AlphabetDecoder(load_alphabet("wabun"))
    decoder.decode(".-..")
    decoder.word_gap()
    assert decoder.decode(DAKUTEN) is None


def test_reset_forgets_last_char():
    decoder = AlphabetDecoder(load_alphabet("wabun"))
    decoder.decode(".-..")
    decoder.reset()
    assert decoder.decode(DAKUTEN) is None


DIGIT_CODES = {str(d): code for code, d in load_alphabet("common").codes.items() if d.isdigit()}


def morse_digits(number: str) -> list[str]:
    return [DIGIT_CODES[d] for d in number]


@pytest.fixture
def telecode_decoder(telecode_db):
    store = TelecodeStore(telecode_db)
    yield TelecodeDecoder(store, load_alphabet("common").codes)
    store.close()


def test_telecode_digits_are_echoed_then_replaced(telecode_decoder):
    emissions = [telecode_decoder.decode(code) for code in morse_digits("0022")]
    assert emissions == [Emission("0"), Emission("0"), Emission("2"), Emission("中", erase=3)]


def test_telecode_full_word(telecode_decoder):
    # simulated text field: really apply the backspaces
    text = ""
    for code in morse_digits("00220948"):
        emission = telecode_decoder.decode(code)
        text = text[: len(text) - emission.erase] + emission.text
    assert text == "中国"


def test_unknown_telecode_keeps_digits(telecode_decoder):
    emissions = [telecode_decoder.decode(code) for code in morse_digits("1234")]
    assert [e.text for e in emissions] == ["1", "2", "3", "4"]
    assert all(e.erase == 0 for e in emissions)
    assert telecode_decoder.pending_digits == ""


def test_non_digit_sequences_are_ignored(telecode_decoder):
    assert telecode_decoder.decode(".-.-.-") is None  # full stop
    assert telecode_decoder.decode(".-") is None  # letter A
    assert telecode_decoder.pending_digits == ""


def test_word_gap_resets_partial_code(telecode_decoder):
    for code in morse_digits("00"):
        telecode_decoder.decode(code)
    assert telecode_decoder.word_gap() is None
    assert telecode_decoder.pending_digits == ""


def test_factory_creates_alphabet_decoder_and_selects():
    manager = DictionaryManager()
    decoder = create_decoder(manager, "greek")
    assert isinstance(decoder, AlphabetDecoder)
    assert manager.active is not None and manager.active.id == "greek"


def test_factory_creates_telecode_decoder(telecode_db):
    manager = DictionaryManager(telecode_db=telecode_db)
    decoder = create_decoder(manager, DictionaryManager.TELECODE_ID)
    assert isinstance(decoder, TelecodeDecoder)


def test_factory_without_db_fails():
    with pytest.raises(AlphabetError, match="Telecode"):
        create_decoder(DictionaryManager(), DictionaryManager.TELECODE_ID)
