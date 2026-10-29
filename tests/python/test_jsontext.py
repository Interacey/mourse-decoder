"""Lenient parsing of pasted texts in alphabet and smiley JSON."""

from __future__ import annotations

import json

import pytest
from mourse_decoder.alphabet_files import parse_alphabet_text
from mourse_decoder.dictionary import AlphabetError
from mourse_decoder.jsontext import escape_stray_quotes, loads

# shape of a real paste: raw line breaks and a straight quote opening a line
PASTED = """{
  "id": "latin-eigen",
  "extends": "common",
  "codes": {
    "--..--..": "Version Information

He said "hello" to them.
"New International Version” and “NIV” are trademarks.
",
    ".": "E"
  }
}"""


def test_pasted_text_with_quotes_and_line_breaks_parses():
    data = loads(PASTED, dict)
    assert data["codes"]["."] == "E"
    assert data["codes"]["--..--.."] == (
        'Version Information\n\nHe said "hello" to them.\n"New International Version” and “NIV” are trademarks.\n'
    )


def test_editor_parser_accepts_the_paste():
    assert parse_alphabet_text(PASTED)["id"] == "latin-eigen"


@pytest.mark.parametrize(
    "text",
    [
        '{"a": "b", "c": ["d", "e"], "f": {"g": "h\\"i"}}',  # already valid, stays unchanged
        '{"a": "x\\\\", "b": "y"}',  # escaped backslash before the closing quote
        '{"a" : "b" }',
    ],
)
def test_valid_json_is_left_alone(text):
    assert escape_stray_quotes(text) == text
    assert loads(text, dict) == json.loads(text)


def test_unrepairable_text_raises_the_original_error():
    # a quote followed by a comma looks like a string end, so this can't be guessed
    with pytest.raises(json.JSONDecodeError):
        loads('{"a": "he said "hi", then left"}', dict)
    with pytest.raises(AlphabetError, match="JSON"):
        parse_alphabet_text("{ kaputt")


def test_duplicate_keys_are_still_found():
    with pytest.raises(AlphabetError, match="doppelt"):
        parse_alphabet_text('{"id": "x", "codes": {".": "Text "mit" Zitat", ".": "E"}}')
