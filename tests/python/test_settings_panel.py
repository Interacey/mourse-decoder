"""Settings page: the typing delay field and the undo shortcut, against a fake engine (skipped without a display)."""

from __future__ import annotations

import pytest
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.i18n import set_language
from mourse_decoder.settings import Settings


class NoCapture:
    def begin_capture(self) -> None: ...
    def cancel_capture(self) -> None: ...
    def poll_capture(self) -> str | None:
        return None


@pytest.fixture
def panel(tk_root, fake_engine, injector):
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings())
    controller.start()
    page = SettingsPanel(tk_root, controller, DictionaryManager(), controller.apply_settings, capture_backend=NoCapture())
    page.pack()
    tk_root.update()
    page.controller, page.engine = controller, fake_engine
    yield page
    page.destroy()


def test_the_delay_field_shows_the_current_value(panel):
    assert panel._vars["typing_delay_ms"].get() == "20"


def test_a_valid_delay_is_applied(panel):
    panel._vars["typing_delay_ms"].set("45")
    panel._changed()
    assert panel.controller.settings.typing_delay_ms == 45
    assert panel._error.cget("text") == ""


@pytest.mark.parametrize(("text", "message"), [("-1", "zwischen 0 und 10000"), ("10001", "zwischen 0 und 10000"), ("x", "ganze Zahl")])
def test_a_bad_delay_is_reported_and_not_applied(panel, text, message):
    panel._vars["typing_delay_ms"].set(text)
    panel._changed()
    assert message in panel._error.cget("text")
    assert panel.controller.settings.typing_delay_ms == 20


def test_the_undo_shortcut_is_applied_to_the_settings_and_the_engine(panel):
    panel._undo_button.set_shortcut("Ctrl+Alt+F8")
    panel._changed()
    assert panel.controller.settings.undo_shortcut == "Ctrl+Alt+F8"
    assert panel.engine.undo_shortcut == "Ctrl+Alt+F8"
    panel._undo_button.set_shortcut("")
    panel._changed()
    assert panel.engine.undo_shortcut == ""


def test_an_undo_shortcut_that_clashes_is_reported(panel):
    panel._undo_button.set_shortcut("Ctrl+Shift+Pause")  # the on/off shortcut
    panel._changed()
    assert panel._error.cget("text")
    assert panel.controller.settings.undo_shortcut == ""


# external trigger


@pytest.fixture
def trigger_panel(tk_root, fake_engine, injector):
    """A settings page fed by fake callables; ``feed.events`` and ``feed.status`` are what it shows on refresh."""
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel

    class Feed:
        events: list = []
        status = "running"

    theme.apply_theme(tk_root, "light")
    set_language("en")
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings())
    controller.start()
    page = SettingsPanel(
        tk_root, controller, DictionaryManager(), controller.apply_settings, capture_backend=NoCapture(),
        trigger_events=lambda: Feed.events, trigger_status=lambda: Feed.status,
    )
    page.pack()
    tk_root.update()
    page.controller, page.feed = controller, Feed
    yield page
    page.destroy()
    set_language("en")


def test_the_trigger_switch_is_saved_through_the_normal_path(panel):
    assert panel._trigger_enabled.get() is False
    panel._trigger_enabled.set(True)
    assert panel.controller.settings.trigger_enabled is True
    panel._trigger_enabled.set(False)
    assert panel.controller.settings.trigger_enabled is False


def test_the_trigger_switch_follows_the_settings_on_refresh(panel):
    panel.controller.apply_settings(panel.controller.settings.replace(trigger_enabled=True))
    panel.refresh()
    assert panel._trigger_enabled.get() is True


def test_without_callables_the_list_is_empty_and_the_status_hidden(panel):
    assert panel._trigger_log.cget("text") == "No requests yet."
    assert not panel._trigger_state.grid_info()


def test_the_event_list_shows_the_injected_events_newest_first(trigger_panel):
    from datetime import datetime

    from mourse_decoder.trigger import TriggerEvent

    first = datetime(2026, 11, 14, 9, 5, 7).timestamp()
    trigger_panel.feed.events = [
        TriggerEvent(first, 57, "ok"), TriggerEvent(first + 60, None, "bad_request"),
        TriggerEvent(first + 120, 3, "rate_limited"),
    ]
    trigger_panel.refresh()
    assert trigger_panel._trigger_log.cget("text").split("\n") == [
        "09:07:07  F3  too many requests", "09:06:07  -  malformed request", "09:05:07  F57  started",
    ]


def test_the_event_list_keeps_the_last_20(trigger_panel):
    from mourse_decoder.trigger import TriggerEvent

    trigger_panel.feed.events = [TriggerEvent(1000.0 + n, n + 1, "ok") for n in range(30)]
    trigger_panel.refresh()
    lines = trigger_panel._trigger_log.cget("text").split("\n")
    assert len(lines) == 20 and lines[0].endswith("F30  started") and lines[-1].endswith("F11  started")


@pytest.mark.parametrize(("status", "shown"), [
    ("running", "Listening for requests."),
    ("unavailable", "Could not start: the pipe name is in use or the user could not be determined."),
])
def test_the_status_line_follows_the_callable(trigger_panel, status, shown):
    trigger_panel.feed.status = status
    trigger_panel.refresh()
    assert trigger_panel._trigger_state.cget("text") == shown
    assert trigger_panel._trigger_state.grid_info()
    trigger_panel.feed.status = "off"
    trigger_panel.refresh()
    assert not trigger_panel._trigger_state.grid_info()


def preview_with(tmp_path, files):
    from mourse_decoder import backup
    from test_backup import build_zip

    return backup.inspect_bundle(build_zip(tmp_path / "in.zip", files), tmp_path / "target" / "MourseDecoder")


def test_the_import_confirmation_names_the_external_trigger_in_the_current_language(panel, tmp_path):
    import json

    keys = {"schema_version": 1, "slots": [{"slot": 13, "trigger": "F13", "macro": "hi", "triggerable": True}]}
    preview = preview_with(tmp_path, {"function_keys.json": json.dumps(keys).encode()})
    text = panel._confirm_text(preview)
    assert "(F13): hi [external trigger]" in text and "external trigger." in text
    set_language("de")
    text = panel._confirm_text(preview)
    assert "[externer Auslöser]" in text and "external trigger" not in text
    set_language("en")


def test_the_import_confirmation_says_that_the_trigger_was_switched_off(panel, tmp_path):
    preview = preview_with(tmp_path, {"settings.json": b'{"trigger_enabled": true}'})
    assert [n.code for n in preview.notes] == ["trigger_off"]
    assert (
        "The external trigger was switched off by the import; switch it on again under Settings if you want it."
        in panel._confirm_text(preview)
    )
