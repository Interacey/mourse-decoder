"""Tests for settings validation, persistence and the form logic."""

from __future__ import annotations

import json

import pytest
from mourse_decoder.settings import TRIGGER_NAMES, Settings, SettingsStore, default_config_dir
from mourse_decoder.ui.form import parse_form


def test_defaults_match_architecture_plan():
    s = Settings()
    assert s.dot_threshold_ms == 200  # under 200 ms is a dot
    assert s.letter_gap_ms == 600  # 600 ms pause
    assert s.word_gap_ms == 1400  # ITU 7:3
    assert s.validate() == []


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"dot_threshold_ms": 10}, "Punkt/Strich-Schwelle"),
        ({"dot_threshold_ms": 99999}, "Punkt/Strich-Schwelle"),
        ({"letter_gap_ms": 50}, "Buchstabenpause"),
        ({"word_gap_ms": -1}, "Wortpause"),
        ({"word_gap_ms": 500}, "länger als die Buchstabenpause"),
        ({"trigger": "Hyper"}, "Unbekannter Taster"),
        ({"alphabet": ""}, "Alphabet"),
        ({"dot_threshold_ms": "200"}, "ganze Zahl"),
        ({"dot_threshold_ms": True}, "ganze Zahl"),  # bool is an int subtype!
        ({"enabled": "ja"}, "enabled"),
    ],
)
def test_validation_errors(changes, fragment):
    errors = Settings().replace(**changes).validate()
    assert any(fragment in e for e in errors), errors


def test_word_gap_zero_disables_relation_check():
    assert Settings(word_gap_ms=0).validate() == []


def test_from_dict_ignores_unknown_and_fills_defaults():
    s = Settings.from_dict({"trigger": "F8", "zukunftsfeld": 1})
    assert s.trigger == "F8"
    assert s.letter_gap_ms == 600


def test_settings_are_immutable():
    with pytest.raises(AttributeError):
        Settings().trigger = "F8"  # type: ignore[misc]


def test_load_without_file_returns_defaults(tmp_path):
    assert SettingsStore(tmp_path).load() == Settings()


def test_save_and_load_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "neu")  # folder doesn't exist yet
    custom = Settings(dot_threshold_ms=150, trigger="MouseRight", alphabet="wabun", enabled=False)
    store.save(custom)
    assert SettingsStore(tmp_path / "neu").load() == custom
    assert not list((tmp_path / "neu").glob("*.tmp")), "Temp-Datei muss weg sein"


def test_saved_file_is_readable_json(tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings(alphabet="cyrillic"))
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["alphabet"] == "cyrillic"


def test_save_rejects_invalid_settings(tmp_path):
    with pytest.raises(ValueError):
        SettingsStore(tmp_path).save(Settings(trigger="Hyper"))
    assert not (tmp_path / "settings.json").exists()


@pytest.mark.parametrize("content", ["{kaputt", "[1, 2]", '{"dot_threshold_ms": 1}', '{"dot_threshold_ms": "x"}'])
def test_broken_file_falls_back_and_is_backed_up(tmp_path, content):
    store = SettingsStore(tmp_path)
    store.path.write_text(content, encoding="utf-8")
    assert store.load() == Settings()
    backup = tmp_path / "settings.json.bak"
    assert backup.read_text(encoding="utf-8") == content
    assert not store.path.exists()


def test_default_config_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_dir() == tmp_path / "mourse-decoder"


def test_default_config_dir_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_config_dir() == tmp_path / "MourseDecoder"


def test_form_parses_valid_values():
    settings, errors = parse_form(
        {"dot_threshold_ms": " 180 ", "letter_gap_ms": "500", "word_gap_ms": "0", "trigger": "F8", "alphabet": "greek"},
        Settings(),
    )
    assert errors == []
    assert settings == Settings(dot_threshold_ms=180, letter_gap_ms=500, word_gap_ms=0, trigger="F8", alphabet="greek")


def test_form_reports_non_numbers():
    settings, errors = parse_form({"dot_threshold_ms": "schnell"}, Settings())
    assert settings is None
    assert errors == ["Punkt/Strich-Schwelle: 'schnell' ist keine ganze Zahl."]


def test_form_runs_full_validation():
    settings, errors = parse_form({"letter_gap_ms": "800", "word_gap_ms": "700"}, Settings())
    assert settings is None
    assert any("Wortpause" in e for e in errors)


def test_form_keeps_untouched_fields():
    base = Settings(alphabet="wabun", enabled=False)
    settings, _ = parse_form({"dot_threshold_ms": "250"}, base)
    assert settings is not None and settings.alphabet == "wabun" and settings.enabled is False


def test_every_trigger_is_valid():
    for name in TRIGGER_NAMES:
        assert Settings(trigger=name).validate() == []
