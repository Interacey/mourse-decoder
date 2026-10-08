"""Tests for text to Morse code."""

from __future__ import annotations

import pytest
from mourse_decoder.dictionary import load_alphabet
from mourse_decoder.translator import encode_text, reverse_table


def test_basic_text_words_and_lines():
    latin = load_alphabet("latin")
    assert encode_text(latin, "SOS").morse == "... --- ..."
    assert encode_text(latin, "sos now").morse == "... --- ... / -. --- .--"
    assert encode_text(latin, "A\nB").morse == ".-\n-..."
    assert encode_text(latin, "  A   B ").morse == ".- / -..."
    assert encode_text(latin, "").morse == ""


def test_digits_and_punctuation_come_from_common():
    assert encode_text(load_alphabet("latin"), "42?").morse == "....- ..--- ..--.."


def test_unknown_characters_are_marked_and_reported_once():
    result = encode_text(load_alphabet("latin"), "A€€B")
    assert result.morse == ".- ? ? -..."
    assert result.unknown == ["€"]


def test_umlauts_depend_on_alphabet():
    assert encode_text(load_alphabet("german"), "Ü").morse == "..--"
    assert encode_text(load_alphabet("latin"), "Ü").unknown == ["Ü"]


def test_decomposed_characters_use_combining_marks():
    wabun = load_alphabet("wabun")
    ga = encode_text(wabun, "ガ")
    ka = encode_text(wabun, "カ")
    assert not ga.unknown and ga.morse.startswith(ka.morse + " ")


@pytest.mark.parametrize("alphabet_id", ["latin", "german", "greek", "cyrillic", "wabun"])
def test_round_trip_through_the_decoder_table(alphabet_id):
    alphabet = load_alphabet(alphabet_id)
    for char in list(alphabet.codes.values())[:25]:
        morse = encode_text(alphabet, char).morse
        assert alphabet.codes.get(morse) == char or morse == min(
            (c for c, v in alphabet.codes.items() if v == char), key=len
        )


def test_shortest_code_wins_for_duplicate_characters():
    latin = load_alphabet("latin")
    codes = [c for c, v in latin.codes.items() if v == "A"]
    assert reverse_table(latin)["A"] == min(codes, key=len)
