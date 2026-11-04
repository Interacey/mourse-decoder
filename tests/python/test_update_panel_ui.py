"""Update page: uninstall questions and the fallback to the releases page (skipped without a display)."""

from __future__ import annotations

import time
import tkinter as tk

import pytest
from mourse_decoder import updater
from mourse_decoder.i18n import set_language
from mourse_decoder.ui import theme, update_panel


@pytest.fixture(scope="module")
def root():
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("kein Display")
    window.withdraw()
    theme.apply_theme(window, "light")
    yield window
    window.destroy()
    theme._images.clear()


@pytest.fixture
def panel(root, tmp_path, monkeypatch):
    set_language("en")
    monkeypatch.setattr(update_panel, "default_config_dir", lambda: tmp_path / "MourseDecoder")
    quits = []
    widget = update_panel.UpdatePanel(root, lambda: quits.append(True))
    widget.quits = quits
    yield widget
    widget.destroy()


def answers(monkeypatch, confirm, data):
    asked, calls = [], []
    monkeypatch.setattr(update_panel.messagebox, "askyesno", lambda *a, **k: asked.append(("confirm", a)) or confirm)
    monkeypatch.setattr(update_panel.messagebox, "askyesnocancel", lambda *a, **k: asked.append(("data", a)) or data)
    monkeypatch.setattr(update_panel.updater, "uninstall", lambda folder, popen=None, delete_data=True: calls.append(delete_data))
    return asked, calls


def test_yes_deletes_program_and_data(panel, monkeypatch, tmp_path):
    (tmp_path / "MourseDecoder").mkdir()
    asked, calls = answers(monkeypatch, True, True)
    panel._uninstall()
    assert calls == [True] and panel.quits == [True]
    assert str(tmp_path / "MourseDecoder") in asked[1][1][1]  # the exact folder is shown


def test_no_deletes_only_the_program(panel, monkeypatch):
    _, calls = answers(monkeypatch, True, False)
    panel._uninstall()
    assert calls == [False] and panel.quits == [True]


def test_cancel_or_closing_the_second_question_aborts_everything(panel, monkeypatch):
    _, calls = answers(monkeypatch, True, None)
    panel._uninstall()
    assert calls == [] and panel.quits == []


def test_declining_the_first_question_asks_nothing_more(panel, monkeypatch):
    asked, calls = answers(monkeypatch, False, True)
    panel._uninstall()
    assert [kind for kind, _ in asked] == ["confirm"] and calls == [] and panel.quits == []


def test_an_error_is_shown_and_the_app_stays_open(panel, monkeypatch):
    answers(monkeypatch, True, True)
    shown = []
    monkeypatch.setattr(update_panel.messagebox, "showerror", lambda *a, **k: shown.append(a))

    def refuse(folder, popen=None, delete_data=True):
        raise updater.UpdateError("nein")

    monkeypatch.setattr(update_panel.updater, "uninstall", refuse)
    panel._uninstall()
    assert shown and panel.quits == []


def test_failure_never_opens_the_browser_by_itself(panel, monkeypatch):
    opened = []
    monkeypatch.setattr(update_panel.webbrowser, "open", opened.append)
    panel._failed("kaputt", True)
    panel._failed("kaputt", False)
    assert opened == []


@pytest.mark.parametrize("open_page", [True, False])
def test_failure_offers_the_page_button_only_when_it_helps(panel, open_page):
    panel._failed("kaputt", open_page)
    assert (panel._page_btn.winfo_manager() == "pack") is open_page
    assert "kaputt" in panel._status.get()


def test_page_button_opens_the_releases_page(panel, monkeypatch):
    opened = []
    monkeypatch.setattr(update_panel.webbrowser, "open", opened.append)
    panel._failed("kaputt", True)
    panel._open_page()
    assert opened == [updater.RELEASES_URL]


def test_page_button_opens_the_page_of_the_known_release(panel, monkeypatch):
    opened = []
    monkeypatch.setattr(update_panel.webbrowser, "open", opened.append)
    panel._release = updater.Release("v9.9.9", "", "https://github.com/e", None, "https://github.com/p")
    panel._failed("kaputt", True)
    panel._open_page()
    assert opened == ["https://github.com/p"]


def test_busy_is_true_from_start_of_work_until_the_result_is_shown(panel, root):
    import threading

    release = threading.Event()
    done = []

    def job():
        release.wait(5)
        return lambda: done.append(True)

    assert panel.busy is False
    panel._work(job, "x")
    assert panel.busy is True
    release.set()
    for _ in range(100):
        root.update()
        if not panel.busy:
            break
        time.sleep(0.05)
    assert panel.busy is False and done == [True]


def test_a_result_arriving_after_the_page_was_destroyed_is_harmless(root, tmp_path, monkeypatch):
    monkeypatch.setattr(update_panel, "default_config_dir", lambda: tmp_path / "MourseDecoder")
    quits = []
    widget = update_panel.UpdatePanel(root, lambda: quits.append(True))
    widget._results.put(lambda: widget._status.set("x"))  # touches the dead widgets
    widget.destroy()
    widget._poll()  # must not raise
    widget._results.put(lambda: quits.append("after install"))
    widget._poll()
    assert quits == ["after install"]  # an install result still quits the app
