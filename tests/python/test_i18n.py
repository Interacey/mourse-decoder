"""Tests for language files and the new settings (language, appearance)."""

from __future__ import annotations

import json
import string

import pytest
from mourse_decoder import i18n
from mourse_decoder.i18n import BUILTIN_LOCALE_DIR, available_languages, set_language, tr
from mourse_decoder.settings import Settings


@pytest.fixture(autouse=True)
def reset_language():
    yield
    set_language("en")


def load(code):
    return json.loads((BUILTIN_LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))["strings"]


def placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_english_is_default_and_listed_first():
    assert set_language("xx") == "en"  # unknown falls back to English
    assert available_languages()[0] == ("en", "English")


def test_required_languages_are_available():
    ids = {code for code, _ in available_languages()}
    assert {"en", "de", "de-saar", "es", "fr", "it", "pt"} <= ids


@pytest.mark.parametrize("code", ["de", "de-saar", "es", "fr", "it", "pt"])
def test_translation_is_complete_and_keeps_placeholders(code):
    english, other = load("en"), load(code)
    assert set(other) == set(english), f"{code}: Schlüssel weichen von en ab"
    for key, text in english.items():
        assert placeholders(other[key]) == placeholders(text), f"{code}: Platzhalter in {key}"


def test_switching_language_and_fallback():
    set_language("de")
    assert tr("nav.settings") == "Einstellungen"
    set_language("de-saar")
    assert tr("nav.settings") == "Eistellunge"
    assert tr("does.not.exist") == "does.not.exist"


def test_placeholders_are_filled_and_bad_ones_survive():
    assert tr("credits.version", v="1.2") == "Version 1.2"
    assert tr("credits.version") == "Version {v}"  # unchanged without values
    assert tr("credits.version", wrong="x") == "Version {v}"  # wrong name: no crash


def test_user_language_file_is_picked_up_and_falls_back(tmp_path):
    (tmp_path / "nl.json").write_text(
        json.dumps({"id": "nl", "name": "Nederlands", "strings": {"nav.settings": "Instellingen"}}), encoding="utf-8"
    )
    assert ("nl", "Nederlands") in available_languages(tmp_path)
    set_language("nl", tmp_path)
    assert tr("nav.settings") == "Instellingen"
    assert tr("nav.live") == "Live Test"  # missing falls back to English
    i18n._user_dir = None


def test_broken_language_file_is_ignored(tmp_path):
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert all(code != "bad" for code, _ in available_languages(tmp_path))


# settings
def test_new_settings_defaults():
    s = Settings()
    assert (s.language, s.theme, s.sidebar_collapsed) == ("en", "system", False)
    assert s.validate() == []


@pytest.mark.parametrize("changes", [{"theme": "neon"}, {"language": ""}, {"sidebar_collapsed": "ja"}])
def test_invalid_new_settings_are_rejected(changes):
    assert Settings().replace(**changes).validate()


def test_old_settings_file_without_new_fields_still_loads():
    s = Settings.from_dict({"dot_threshold_ms": 250})
    assert s.language == "en" and s.dot_threshold_ms == 250
