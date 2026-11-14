"""``--trigger ID`` on the command line: the client path runs before anything that a second process must not touch."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest
from mourse_decoder import app, trigger, trigger_pipe


@pytest.fixture
def spies(monkeypatch):
    """Everything the program start does before it is the program; any call is a failure. Returns the client calls."""

    def forbidden(name):
        def call(*args, **kwargs):
            raise AssertionError(f"{name} must not run on the --trigger path")

        return call

    calls = []
    monkeypatch.setattr(app, "_exit_when_launcher_dies", lambda: None)
    monkeypatch.setattr(app, "acquire_single_instance", forbidden("acquire_single_instance"))
    monkeypatch.setattr(app, "load_native", forbidden("load_native"))
    monkeypatch.setattr(app, "SettingsStore", forbidden("SettingsStore"))
    monkeypatch.setattr(logging, "basicConfig", forbidden("logging.basicConfig"))
    monkeypatch.setattr(trigger_pipe, "client_main", lambda id_text: calls.append(id_text) or 14)
    return calls


@pytest.mark.parametrize("id_text", ["F5", "F100", "f5", "F0", "F101", "x", ""])
def test_trigger_hands_the_id_to_the_client_and_returns_its_exit_code(spies, id_text):
    assert app.main(["--trigger", id_text]) == 14
    assert spies == [id_text]  # the client validates; the argument check does not


def test_trigger_with_verbose_is_allowed(spies):
    assert app.main(["--trigger", "F5", "-v"]) == 14


@pytest.mark.parametrize("flag", ["--headless", "--dry-run", "--minimized"])
def test_trigger_cannot_be_combined_with_the_server_modes(spies, capsys, flag):
    with pytest.raises(SystemExit) as stop:
        app.main(["--trigger", "F5", flag])
    assert stop.value.code == 2
    assert spies == []
    assert "cannot be combined" in capsys.readouterr().err


def test_trigger_needs_an_id(spies, capsys):
    with pytest.raises(SystemExit) as stop:
        app.main(["--trigger"])
    assert stop.value.code == 2 and spies == []


def test_windowed_exe_gives_the_client_its_missing_streams_back(spies, monkeypatch):
    """_prepare_windows replaces a missing stdout by a null file; the client could then never attach a console."""
    seen = []
    monkeypatch.setattr(trigger_pipe, "client_main", lambda id_text: seen.append((sys.stdout, sys.stderr)) or 0)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert app.main(["--trigger", "F5"]) == 0
    assert seen == [(None, None)]


def test_streams_that_exist_stay_as_they_are(spies, monkeypatch):
    seen = []
    monkeypatch.setattr(trigger_pipe, "client_main", lambda id_text: seen.append((sys.stdout, sys.stderr)) or 0)
    before = (sys.stdout, sys.stderr)
    app.main(["--trigger", "F5"])
    assert seen == [before]


def test_help_documents_every_exit_code_and_how_to_wait_for_it(spies, capsys):
    with pytest.raises(SystemExit) as stop:
        app.main(["--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    assert "--trigger ID" in text
    assert "Start-Process -Wait -PassThru" in text and "start /wait" in text
    for code in {2, *trigger.EXIT_CODES.values()} - {0}:
        assert re.search(rf"\b{code}\b", text), code
    assert re.search(r"\b0 started\b", text)


@pytest.fixture
def server_path(monkeypatch):
    monkeypatch.setattr(app, "_exit_when_launcher_dies", lambda: None)
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)


def test_server_path_keeps_exit_code_2_when_the_native_module_is_missing(server_path, monkeypatch, capsys):
    def missing():
        raise RuntimeError("no native module")

    monkeypatch.setattr(app, "load_native", missing)
    assert app.main(["--dry-run"]) == 2  # --dry-run skips the single-instance check


def test_server_path_keeps_exit_code_3_for_a_second_instance(server_path, monkeypatch):
    monkeypatch.setattr(app, "acquire_single_instance", lambda: False)
    monkeypatch.setattr(app, "_tell_already_running", lambda: None)
    assert app.main([]) == 3


def test_help_has_the_powershell_example_that_returns_the_exit_code(spies, capsys):
    with pytest.raises(SystemExit):
        app.main(["--help"])
    text = capsys.readouterr().out
    assert r"$p = Start-Process -Wait -PassThru .\MourseDecoder.exe '--trigger F57'; $p.ExitCode" in text
    assert "unsupported" in text  # code 16 covers it: no Windows pipe API


def test_readme_has_the_pipe_path_and_every_client_code():
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    assert r"\\.\pipe\MourseDecoder-<SID>" in readme
    assert not re.search(r"(?<!\\)\\\.\\pipe", readme), "a pipe path must start with two backslashes"
    for name in trigger.EXIT_CODES:
        assert f"`error {name}`" in readme or f"`{name}`" in readme, name
