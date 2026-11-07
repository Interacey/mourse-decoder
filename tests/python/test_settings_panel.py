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
