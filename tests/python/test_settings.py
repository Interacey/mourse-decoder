"""Tests for settings validation, persistence and the form logic."""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

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
        assert Settings(trigger=name, block_shortcut="").validate() == []  # Pause clashes with Ctrl+Shift+Pause


def test_old_file_with_clashing_shortcut_keeps_settings(tmp_path):
    """Saved before the clash check: trigger Pause fires with Ctrl+Shift+Pause. Load clears the shortcut only."""
    store = SettingsStore(tmp_path)
    data = Settings().to_dict()
    data.update(trigger="Pause", separate_dash_key=True, dash_trigger="F9", dot_threshold_ms=222, alphabet="morse")
    data["block_shortcut"] = "Ctrl+Shift+Pause"
    (tmp_path / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = store.load()
    assert loaded.block_shortcut == ""
    assert (loaded.trigger, loaded.dot_threshold_ms, loaded.alphabet) == ("Pause", 222, "morse")
    assert not (tmp_path / "settings.json.bak").exists()  # file kept, not moved away


def test_clash_without_shortcut_cause_still_falls_back(tmp_path):
    data = Settings().to_dict()
    data.update(trigger="F9", separate_dash_key=True, dash_trigger="F9")
    (tmp_path / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    assert SettingsStore(tmp_path).load() == Settings()
    assert (tmp_path / "settings.json.bak").exists()


# trigger_enabled (external macro trigger)

# values that need no valid default: the field is checked as a boolean like the other switches
NOT_BOOLEAN = ["ja", "true", 1, 0, None, [], {}]


def test_trigger_enabled_is_off_by_default():
    assert Settings().trigger_enabled is False
    assert Settings().to_dict()["trigger_enabled"] is False
    assert Settings(trigger_enabled=True).validate() == []


def test_from_dict_of_an_old_dict_defaults_trigger_enabled_to_false():
    assert Settings.from_dict({}).trigger_enabled is False
    assert Settings.from_dict({"trigger": "F8", "enabled": False}).trigger_enabled is False
    old = Settings().to_dict()
    del old["trigger_enabled"]
    assert Settings.from_dict(old) == Settings()
    assert Settings.from_dict({"trigger_enabled": True}).trigger_enabled is True


@pytest.mark.parametrize("value", NOT_BOOLEAN)
def test_trigger_enabled_must_be_a_boolean(value):
    assert Settings(trigger_enabled=value).validate() == ["'trigger_enabled' muss true oder false sein."]


@pytest.mark.parametrize("value", NOT_BOOLEAN)
def test_a_file_with_a_non_boolean_trigger_enabled_is_dropped_like_other_booleans(tmp_path, value):
    data = Settings().to_dict()
    data.update(trigger_enabled=value, letter_gap_ms=555)
    text = json.dumps(data)
    (tmp_path / "settings.json").write_text(text, encoding="utf-8")
    store = SettingsStore(tmp_path)
    assert store.load() == Settings()
    assert (tmp_path / "settings.json.bak").read_text(encoding="utf-8") == text
    assert store.warnings and store.warnings[0][0] == "startup.settings_discarded"


def test_a_file_without_the_key_loads_with_false_and_is_not_rewritten(tmp_path):
    text = '{"alphabet": "de", "letter_gap_ms": 700}'
    (tmp_path / "settings.json").write_text(text, encoding="utf-8")
    loaded = SettingsStore(tmp_path).load()
    assert loaded.trigger_enabled is False and loaded.alphabet == "de"
    assert (tmp_path / "settings.json").read_text(encoding="utf-8") == text
    assert not (tmp_path / "settings.json.bak").exists()


def test_a_saved_file_holds_the_key_and_round_trips_it(tmp_path):
    store = SettingsStore(tmp_path)
    store.save(Settings())
    assert json.loads(store.path.read_text(encoding="utf-8"))["trigger_enabled"] is False
    store.save(Settings(trigger_enabled=True))
    assert json.loads(store.path.read_text(encoding="utf-8"))["trigger_enabled"] is True
    assert store.load() == Settings(trigger_enabled=True)


def test_a_file_with_unknown_future_keys_still_loads(tmp_path):
    data = Settings(trigger_enabled=True).to_dict()
    data.update(zukunft={"a": 1}, trigger_port=1234)
    (tmp_path / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    store = SettingsStore(tmp_path)
    assert store.load() == Settings(trigger_enabled=True)
    assert store.warnings == [] and not (tmp_path / "settings.json.bak").exists()


def _settings_samples() -> list[tuple[str, str]]:
    """Every string in the test files that is a JSON object whose keys are all setting names: the sample
    settings.json files the other tests write (and ``{}``)."""
    names = {f.name for f in dataclasses.fields(Settings)}
    found: dict[str, str] = {}
    for path in sorted(Path(__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
                try:
                    parsed = json.loads(node.value)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and set(parsed) <= names and "trigger_enabled" not in parsed:
                    found[json.dumps(parsed, sort_keys=True)] = f"{path.name}:{node.lineno}"
    return [(source, text) for text, source in found.items()]


def test_every_settings_sample_of_the_other_tests_loads_with_trigger_enabled_false(tmp_path):
    samples = _settings_samples()
    assert len(samples) >= 10, "the scan must find the samples (an empty scan would pass)"
    for source, text in samples:
        (tmp_path / "settings.json").write_text(text, encoding="utf-8")
        assert SettingsStore(tmp_path).load().trigger_enabled is False, (source, text)
        assert Settings.from_dict(json.loads(text)).trigger_enabled is False, (source, text)
