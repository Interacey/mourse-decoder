"""The {Scroll Down}, {Scroll Up} and {Scroll Stop} commands: parsing and the import warning."""

from __future__ import annotations

import json

import pytest
from mourse_decoder import macros


def no_shortcut(combo):
    raise ValueError(combo)  # a scroll command must never get here


def parse(text):
    return macros.parse(text, no_shortcut)


def write_set(tmp_path, codes):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"id": "x", "codes": codes}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("{Scroll Down}", "down"),
        ("{ScrollDown}", "down"),
        ("{scroll down}", "down"),
        ("{Scroll Runter}", "down"),
        ("{Scroll Up}", "up"),
        ("{ Scroll  Up }", "up"),
        ("{ScrollHoch}", "up"),
        ("{Scroll Stop}", "stop"),
        ("{ScrollEnd}", "stop"),
        ("{Scroll Stopp}", "stop"),
        ("{Scroll Ende}", "stop"),
    ],
)
def test_scroll_spellings(text, kind):
    assert parse(text) == [("scroll", kind)]


def test_scroll_between_text():
    assert parse("a{Scroll Down}b") == [("text", "a"), ("scroll", "down"), ("text", "b")]


def test_scroll_inside_a_loop_is_expanded():
    assert parse("{Loop 3}{Scroll Down}{EndLoop}") == [("scroll", "down")] * 3


@pytest.mark.parametrize("text", ["{Scroll Down 3}", "{Scroll Sideways}"])
def test_bad_scroll_commands_are_errors(text):
    with pytest.raises(macros.MacroError):
        parse(text)


def test_scroll_commands_are_listed_for_the_import_warning(tmp_path):
    path = write_set(
        tmp_path,
        {"--..--..": "{Scroll Down}", "--..-.-.": "{scrollup}", "--...-..": "x{Scroll Ende}"},
    )
    assert macros.commands_in_file(path) == ["{Scroll Down}", "{Scroll Up}", "{Scroll Stop}"]
