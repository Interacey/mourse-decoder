"""Function keys page: the pure view logic, and the panel against a fake engine (skipped without a display)."""

from __future__ import annotations

import json

import pytest
from mourse_decoder import function_keys as fk
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.function_keys import FunctionKeys, FunctionKeysStore, Slot
from mourse_decoder.i18n import set_language
from mourse_decoder.settings import Settings
from mourse_decoder.ui import function_keys_view as view

# view


def slot(number=57, **changes) -> Slot:
    return Slot(number, **{"trigger": "Ctrl+F5", **changes})


def test_virtual_starts_after_f24():
    assert [view.is_virtual(n) for n in (1, 13, 24, 25, 100)] == [False, False, False, True, True]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"macro": "a"}, "active"),
        ({"macro": "a", "enabled": False}, "off"),
        ({"macro": "  "}, "empty"),
        ({"macro": "a", "trigger": ""}, "notrigger"),
        ({"macro": "", "enabled": False}, "off"),
    ],
)
def test_status(changes, expected):
    assert view.status(slot(**changes)) == expected


def test_preview_prefers_the_label_and_shows_line_breaks():
    assert view.preview(slot(label="Greeting", macro="x")) == "Greeting"
    assert view.preview(slot(macro="Hi\r\nthere\nyou\tall")) == f"Hi{view.LINE_BREAK}there{view.LINE_BREAK}you all"


def test_preview_is_cut_with_an_ellipsis():
    text = view.preview(slot(macro="x" * 200), limit=20)
    assert len(text) == 20 and text.endswith("…")
    assert view.preview(slot(macro="x" * 20), limit=20) == "x" * 20


def test_matches_name_label_and_macro_ignoring_case():
    s = slot(57, label="Signature", macro="Kind regards")
    assert all(view.matches(s, q) for q in ("", "  ", "f57", "SIGN", "regards"))
    assert not view.matches(s, "f58")


def keys_with(**macros_by_slot: str) -> FunctionKeys:
    keys = FunctionKeys()
    for name, macro in macros_by_slot.items():
        keys = keys.replace_slot(keys.slot(int(name[1:])).replace(macro=macro))
    return keys


def test_filter_only_used_and_query():
    keys = keys_with(F13="hello", F57="world")
    keys = keys.replace_slot(keys.slot(40).replace(label="note"))
    assert len(view.filter_slots(keys)) == 100
    assert [s.slot for s in view.filter_slots(keys, only_used=True)] == [13, 40, 57]
    assert [s.slot for s in view.filter_slots(keys, "wor")] == [57]
    assert [s.slot for s in view.filter_slots(keys, "o", only_used=True)] == [13, 40, 57]
    assert view.filter_slots(keys, "nothing like this") == []


def test_errors_for_slot_does_not_mix_up_f5_and_f57():
    errors = ["F5: a", "F57: b", "F5 und F57 haben denselben Auslöser (F13).", "F157 x", "xF5"]
    assert view.errors_for_slot(errors, 5) == ["F5: a", "F5 und F57 haben denselben Auslöser (F13)."]
    assert view.errors_for_slot(errors, 57) == ["F57: b", "F5 und F57 haben denselben Auslöser (F13)."]


def test_row_translates_nothing_yet():
    assert view.row(slot(57, macro="a")) == ("F57", "virtual", "Ctrl + F5", "a", "active")


def test_every_view_status_and_kind_has_a_locale_string():
    from mourse_decoder.i18n import BUILTIN_LOCALE_DIR

    strings = json.loads((BUILTIN_LOCALE_DIR / "en.json").read_text(encoding="utf-8"))["strings"]
    for key in ("real", "virtual"):
        assert f"functionkeys.type.{key}" in strings
    for key in ("active", "off", "notrigger", "empty"):
        assert f"functionkeys.status.{key}" in strings


# locales


def test_function_keys_strings_exist_in_every_language():
    from mourse_decoder.i18n import BUILTIN_LOCALE_DIR

    def load(code):
        return json.loads((BUILTIN_LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))["strings"]

    english = {k for k in load("en") if k.startswith("functionkeys.")} | {"nav.functionkeys"}
    assert len(english) > 30
    for code in ("de", "de-saar", "es", "fr", "it", "pt"):
        assert english <= set(load(code)), code


# panel


@pytest.fixture(scope="module")
def root(tk_root):
    from mourse_decoder.ui import theme

    theme.apply_theme(tk_root, "light")
    return tk_root


class FakeToast:
    def __init__(self) -> None:
        self.shown: list[str] = []

    def show(self, text: str, on: bool = True) -> None:
        self.shown.append(text)


class NoCapture:
    def begin_capture(self) -> None: ...
    def cancel_capture(self) -> None: ...
    def poll_capture(self) -> str | None:
        return None


@pytest.fixture
def page(root, fake_engine, injector, tmp_path):
    from mourse_decoder.ui.function_keys_panel import FunctionKeysPanel

    set_language("en")
    (tmp_path / "function_keys.json").write_text(
        json.dumps({"schema_version": 1, "slots": [{"slot": 13, "trigger": "F13", "macro": "Hello", "label": "Hi"}]}),
        encoding="utf-8",
    )
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings())
    controller.start()
    store = FunctionKeysStore(tmp_path)
    toast = FakeToast()
    panel = FunctionKeysPanel(root, controller, store, capture_backend=NoCapture(), toast=toast)
    panel.pack()
    root.update()
    controller.apply_function_keys(panel._keys)  # what the app does at startup
    panel.toast, panel.store, panel.controller, panel.engine, panel.dir = toast, store, controller, fake_engine, tmp_path
    yield panel
    panel.destroy()


def saved_slots(page) -> dict[int, dict]:
    return {s["slot"]: s for s in json.loads((page.dir / "function_keys.json").read_text(encoding="utf-8"))["slots"]}


def select(page, number: int) -> None:
    page._tree.selection_set(str(number))
    page.update()


def type_macro(page, text: str) -> None:
    page._macro.delete("1.0", "end")
    page._macro.insert("1.0", text)
    page.update()
    page._validate()  # the page does this after a short pause


def test_list_shows_all_slots_and_opens_the_first_used_one(page):
    assert len(page._tree.get_children()) == 100
    assert page._number == 13
    assert page._macro.get("1.0", "end-1c") == "Hello"
    assert page._label_var.get() == "Hi"
    assert page._tree.item("13", "values")[0] == "F13"


def test_filter_narrows_the_list_and_keeps_the_editor(page):
    page._only_used.set(True)
    assert page._tree.get_children() == ("13",)
    page._only_used.set(False)
    page._query.set("f9")
    assert page._tree.get_children() == tuple(str(n) for n in (9, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99))
    assert page._number == 13


def test_selecting_a_slot_loads_it(page):
    select(page, 57)
    assert page._number == 57
    assert page._trigger.shortcut == fk.default_trigger(57)
    assert page._macro.get("1.0", "end-1c") == ""


def test_save_installs_the_slot_and_writes_the_file(page):
    select(page, 57)
    type_macro(page, "Kind regards{Enter}")
    page._label_var.set("Signature")
    page._save()
    assert page.controller.function_keys.slot(57).macro == "Kind regards{Enter}"
    assert (57, fk.default_trigger(57), True) in page.engine.function_keys
    assert saved_slots(page)[57]["macro"] == "Kind regards{Enter}"
    assert page._tree.item("57", "values")[3] == "Signature"
    assert page.toast.shown == ["F57 saved"]
    assert not page._dirty()


def test_broken_macro_shows_the_error_and_cannot_be_saved(page):
    select(page, 57)
    type_macro(page, "{Loop 3 i}x")
    assert "EndLoop" in page._error.cget("text")
    assert page._error.grid_info()
    page._save()  # the button is disabled, but even a direct call must not store anything
    assert page.controller.function_keys.slot(57).macro == ""
    assert 57 not in saved_slots(page)
    type_macro(page, "fine")
    assert page._error.cget("text") == "" and not page._error.grid_info()


def test_trigger_clash_with_another_slot_is_reported(page):
    select(page, 14)
    page._enabled.set(True)
    type_macro(page, "x")
    page._trigger.set_shortcut("F13")
    page._validate()
    assert "F13 und F14" in page._error.cget("text")


def test_failed_write_puts_the_old_slots_back(page, monkeypatch):
    select(page, 57)
    type_macro(page, "new")

    def fail(_keys):
        raise OSError("Platte voll")

    monkeypatch.setattr(page.store, "save", fail)
    page._save()
    assert "Platte voll" in page._error.cget("text")
    assert page.controller.function_keys.slot(57).macro == ""
    assert page.engine.function_keys == [(13, "F13", True)]
    assert page._keys.slot(57).macro == ""


def test_newer_file_is_shown_but_not_saved(page):
    page.store.read_only = True
    select(page, 57)
    type_macro(page, "new")
    page._save()
    assert page.controller.function_keys.slot(57).macro == ""
    assert not page._save_button._enabled


def test_default_button_and_reset(page, monkeypatch):
    monkeypatch.setattr("mourse_decoder.ui.function_keys_panel.messagebox.askyesno", lambda *a, **k: True)
    select(page, 13)
    page._trigger.set_shortcut("Ctrl+F5")
    page._default_trigger()
    assert page._trigger.shortcut == "F13"
    page._reset()  # slot 13 holds a macro: back to nothing
    assert page.controller.function_keys.slot(13).macro == ""
    assert 13 not in saved_slots(page)
    assert page._macro.get("1.0", "end-1c") == ""


def test_switching_slot_asks_before_dropping_changes(page, monkeypatch):
    asked = []
    monkeypatch.setattr(
        "mourse_decoder.ui.function_keys_panel.messagebox.askyesno", lambda *a, **k: asked.append(a) or False
    )
    type_macro(page, "changed")
    select(page, 40)
    assert asked and page._number == 13
    assert page._macro.get("1.0", "end-1c") == "changed"
    assert page._tree.selection() == ("13",)


def test_test_button_runs_the_saved_slot_after_the_delay(page, monkeypatch):
    monkeypatch.setattr("mourse_decoder.ui.function_keys_panel.TEST_DELAY_MS", 1)
    page.controller.run_slot = lambda n: None if n == 13 else "nein"
    page._test()
    assert page.toast.shown[0].startswith("F13 runs in")
    page.update()
    page.after(30, page.quit)
    page.mainloop()
    assert page.toast.shown[-1] == "F13 started"


def test_notice_lists_load_warnings(root, fake_engine, injector, tmp_path):
    from mourse_decoder.ui.function_keys_panel import FunctionKeysPanel

    (tmp_path / "function_keys.json").write_text('{"schema_version": 9, "slots": []}', encoding="utf-8")
    controller = MorseController(fake_engine, injector, DictionaryManager(), Settings())
    controller.start()
    panel = FunctionKeysPanel(root, controller, FunctionKeysStore(tmp_path), capture_backend=NoCapture(), toast=FakeToast())
    panel.pack()
    root.update()
    try:
        assert panel._notice.winfo_ismapped()
        assert "schema_version 9" in panel._notice_text.cget("text")
    finally:
        panel.destroy()




def test_the_delay_field_is_empty_for_a_slot_without_its_own_value(page):
    assert page._delay_var.get() == ""
    assert page._editor_slot().typing_delay_ms is None


def test_saving_a_delay_writes_it_and_reloads_it(page):
    select(page, 57)
    type_macro(page, "text")
    page._delay_var.set("80")
    page._validate()
    assert page._dirty()
    page._save()
    assert page.controller.function_keys.slot(57).typing_delay_ms == 80
    assert saved_slots(page)[57]["typing_delay_ms"] == 80
    select(page, 13)
    select(page, 57)
    assert page._delay_var.get() == "80"


def test_emptying_the_field_goes_back_to_the_global_value(page):
    select(page, 57)
    type_macro(page, "text")
    page._delay_var.set("80")
    page._save()
    page._delay_var.set("  ")
    page._save()
    assert page.controller.function_keys.slot(57).typing_delay_ms is None
    assert "typing_delay_ms" not in saved_slots(page)[57]


@pytest.mark.parametrize(("text", "message"), [("abc", "ganze Zahl"), ("-1", "zwischen 0 und 10000"), ("10001", "zwischen 0 und 10000")])
def test_a_bad_delay_is_shown_and_cannot_be_saved(page, text, message):
    select(page, 57)
    type_macro(page, "text")
    page._delay_var.set(text)
    page._validate()
    assert message in page._error.cget("text")
    page._save()
    assert page.controller.function_keys.slot(57).typing_delay_ms is None
    assert 57 not in saved_slots(page)


def test_changing_only_the_delay_makes_the_slot_dirty(page):
    assert not page._dirty()
    page._delay_var.set("60")
    assert page._dirty()
