"""Review round B: startup fallback, single instance, stdout encoding."""

from __future__ import annotations

import io
import sys
import uuid

import pytest
from mourse_decoder import app
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.settings import Settings


def test_unloadable_alphabet_falls_back_to_latin(fake_engine, injector):
    controller, warnings = app.build_controller(fake_engine, injector, DictionaryManager(), Settings(alphabet="gone"))
    controller.start()
    assert controller.settings.alphabet == "latin"
    assert len(warnings) == 1 and "gone" in warnings[0] and "latin" in warnings[0]
    fake_engine.letters(".-")
    assert injector.buffer == "A"


def test_loadable_alphabet_gives_no_warning(fake_engine, injector):
    controller, warnings = app.build_controller(fake_engine, injector, DictionaryManager(), Settings())
    assert warnings == [] and controller.settings.alphabet == "latin"


def test_telecode_without_database_falls_back(fake_engine, injector):
    controller, warnings = app.build_controller(
        fake_engine, injector, DictionaryManager(), Settings(alphabet="telecode")
    )
    assert controller.settings.alphabet == "latin" and warnings


@pytest.mark.skipif(sys.platform != "win32", reason="named mutex is Windows only")
def test_second_instance_is_refused():
    name = f"Local\\MourseDecoderTest-{uuid.uuid4().hex}"
    assert app.acquire_single_instance(name) is True
    assert app.acquire_single_instance(name) is False


def test_single_instance_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert app.acquire_single_instance("x") is True
    assert app.acquire_single_instance("x") is True


def test_streams_tolerate_unencodable_text(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", io.StringIO())  # has no reconfigure on a StringIO: must not fail
    app._tolerate_unencodable_output()
    app._ConsoleListener().on_letter(".-", "ガ")
    stream.flush()
    assert b"?" in raw.getvalue()


def test_update_result_is_shown_once(tmp_path):
    from mourse_decoder import updater

    updater.result_path(tmp_path).write_text("unknown", encoding="utf-8")
    shown = []
    app.show_update_result(tmp_path, show=lambda title, msg: shown.append((title, msg)))
    app.show_update_result(tmp_path, show=lambda title, msg: shown.append((title, msg)))
    assert len(shown) == 1 and shown[0][1]


def test_no_update_result_shows_nothing(tmp_path):
    shown = []
    app.show_update_result(tmp_path, show=lambda *a: shown.append(a))
    assert shown == []
