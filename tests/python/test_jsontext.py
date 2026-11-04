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


# --- repair() ---------------------------------------------------------------

from mourse_decoder.alphabet_files import pretty_alphabet_text, save_alphabet_text  # noqa: E402
from mourse_decoder.dictionary import DictionaryManager  # noqa: E402
from mourse_decoder.jsontext import repair  # noqa: E402


def test_repair_leaves_valid_json_alone():
    assert repair('{"a": 1}') == ('{"a": 1}', [])


@pytest.mark.parametrize(
    ("broken", "expected", "fix"),
    [
        ('{"a": 1, "b": [1, 2,],}', {"a": 1, "b": [1, 2]}, "Kommas"),
        ("{'a': 'x \"y\" z'}", {"a": 'x "y" z'}, "einfache"),
        ('{"a": 1, // note\n "b": 2 /* c */}', {"a": 1, "b": 2}, "Kommentare"),
        ("{a: 1, b_2: true}", {"a": 1, "b_2": True}, "Schlüssel"),
        ('{"a": True, "b": None}', {"a": True, "b": None}, "True"),
        ('{"a": 1\n "b": 2}', {"a": 1, "b": 2}, "fehlende Kommas"),
        ('{"a": [1, 2], "b": {"c": "d"', {"a": [1, 2], "b": {"c": "d"}}, "schließende"),
        ('\ufeff{"a": 1,}', {"a": 1}, "Kommas"),
    ],
)
def test_repair_fixes_unambiguous_slips(broken, expected, fix):
    fixed, fixes = repair(broken)
    assert json.loads(fixed) == expected
    assert any(fix in f for f in fixes)


def test_repair_comment_marks_inside_strings_survive():
    fixed, _ = repair('{"url": "http://x//y", "a": 1,}')
    assert json.loads(fixed)["url"] == "http://x//y"


@pytest.mark.parametrize("broken", ['{"a": }', '{"a" 1}', "{ kaputt", '{"a": nope}', '{"a": 1}}'])
def test_repair_refuses_ambiguous_text_with_original_error(broken):
    with pytest.raises(json.JSONDecodeError):
        repair(broken)


def test_save_repairs_and_reports(tmp_path):
    builtin, user = tmp_path / "b", tmp_path / "u"
    builtin.mkdir()
    (builtin / "common.json").write_text('{"id": "common", "name": "c", "codes": {".": "E"}}', encoding="utf-8")
    manager = DictionaryManager(builtin, user_directory=user)
    fixes: list[str] = []
    info = save_alphabet_text(manager, "{'id': 'mein', 'name': 'M', 'codes': {'-': 'T',},}", fixes=fixes)
    assert info.id == "mein"
    assert len(fixes) == 2
    assert json.loads((user / "mein.json").read_text(encoding="utf-8"))["codes"] == {"-": "T"}
    with pytest.raises(AlphabetError, match="JSON"):  # without a fixes list nothing is repaired
        save_alphabet_text(manager, "{'id': 'x'}")


def test_pretty_text_wraps_one_line_json_only():
    one_line = json.dumps({"id": "x", "codes": {f"{'.' * i}-": "A" * 5 for i in range(1, 30)}})
    pretty = pretty_alphabet_text(one_line)
    assert pretty.count("\n") > 10 and json.loads(pretty) == json.loads(one_line)
    short = '{"id": "x"}'
    assert pretty_alphabet_text(short) == short
    assert pretty_alphabet_text("{ kaputt" + " " * 200) == "{ kaputt" + " " * 200
