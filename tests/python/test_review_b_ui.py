"""Review round B: tray poll loop and main window rebuild, with fakes (no Tk window is created)."""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

try:
    import mourse_decoder.ui.main_window as main_mod
    import mourse_decoder.ui.tray as tray_mod
except Exception as exc:  # pystray raises Xlib's DisplayNameError (not ImportError) without an X display
    pytest.skip(f"UI modules not importable here: {exc!r}", allow_module_level=True)


class Root:
    def __init__(self):
        self.after_calls = []

    def after(self, ms, func=None):
        self.after_calls.append((ms, func))


class Raising:
    def __init__(self, name, log):
        self.name, self.log = name, log

    def __call__(self):
        self.log.append(self.name)
        raise RuntimeError(self.name)


def make_tray(hook_error=None):
    tray = object.__new__(tray_mod.TrayApp)
    log: list[str] = []
    tray._ui_queue = queue.Queue()
    tray._root = Root()
    tray._controller = SimpleNamespace(
        poll_active_toggle=Raising("block", log),
        poll_cycle=Raising("cycle", log),
        poll_undo=Raising("undo", log),
        poll_function_keys=Raising("keys", log),
    )
    tray._toast = SimpleNamespace(shown=[], show=lambda *a: tray._toast.shown.append(a))
    tray._icon = SimpleNamespace(notified=[], notify=lambda *a: tray._icon.notified.append(a))
    tray._hook_error = hook_error
    tray._hook_error_shown = False
    return tray, log


def test_a_failing_poll_does_not_stop_the_loop():
    tray, log = make_tray()
    tray._ui_queue.put(lambda: 1 / 0)
    tray._drain_queue()
    assert log == ["block", "cycle", "undo", "keys"]  # every poll ran although each raised
    assert len(tray._root.after_calls) == 1


def test_dead_hook_is_reported_once():
    tray, _ = make_tray(hook_error=lambda: "Hook gestorben")
    tray._drain_queue()
    tray._drain_queue()
    assert [n[0] for n in tray._icon.notified] == ["Hook gestorben"]
    assert len(tray._toast.shown) == 1


def test_no_hook_error_reports_nothing():
    tray, _ = make_tray(hook_error=lambda: None)
    tray._drain_queue()
    assert tray._icon.notified == []


def test_raising_hook_probe_does_not_stop_the_loop():
    def boom():
        raise RuntimeError("x")

    tray, _ = make_tray(hook_error=boom)
    tray._drain_queue()
    assert len(tray._root.after_calls) == 1


def make_window(busy):
    win = object.__new__(main_mod.MainWindow)
    win._root = Root()
    win._rebuild_pending = True
    win._pages = {"update": SimpleNamespace(busy=busy)}
    win.builds = 0
    win._build = lambda: setattr(win, "builds", win.builds + 1)
    win._on_alphabets_changed = lambda: None
    return win


def test_rebuild_waits_while_an_update_runs():
    win = make_window(busy=True)
    win._rebuild()
    assert win.builds == 0 and win._rebuild_pending is True
    assert len(win._root.after_calls) == 1


def test_rebuild_runs_when_no_update_is_busy():
    win = make_window(busy=False)
    win._rebuild()
    assert win.builds == 1 and win._rebuild_pending is False


def test_rebuild_runs_without_an_update_page():
    win = make_window(busy=False)
    win._pages = {}
    win._rebuild()
    assert win.builds == 1


def test_rebuild_uses_the_public_busy_property():
    from mourse_decoder.ui.update_panel import UpdatePanel

    assert isinstance(UpdatePanel.busy, property)




def undo_tray(poll_undo=lambda: 0, undo_last_output=None):
    tray, _ = make_tray()
    tray._controller = SimpleNamespace(poll_undo=poll_undo, undo_last_output=undo_last_output)
    return tray


def test_pressing_the_undo_shortcut_starts_one_undo(monkeypatch):
    tray = undo_tray(poll_undo=lambda: 3)
    started = []
    monkeypatch.setattr(tray, "_undo", lambda: started.append(1), raising=False)
    tray._poll_undo()
    assert started == [1], "three presses in one tick are one undo"
    tray._controller.poll_undo = lambda: 0
    tray._poll_undo()
    assert started == [1]


def run_undo(result):
    from mourse_decoder.controller import UndoResult
    from mourse_decoder.i18n import set_language

    set_language("en")
    tray = undo_tray(undo_last_output=lambda: UndoResult(*result))
    tray._run_undo()
    while not tray._ui_queue.empty():
        tray._ui_queue.get()()
    return tray._toast.shown


@pytest.mark.parametrize(
    ("result", "shown"),
    [
        ((4,), [("4 characters deleted", True)]),
        ((0, "nothing"), [("Nothing to undo.", False)]),
        ((0, "busy"), [("A macro is still typing.", False)]),
        ((0, "off"), [("Morse input is off.", False)]),
        ((0, "modifiers"), [("Ctrl, Alt, Shift or Win are still held.", False)]),
    ],
)
def test_the_undo_result_is_shown_as_a_message(result, shown):
    assert run_undo(result) == shown


def test_a_failing_undo_shows_the_error():
    from mourse_decoder.i18n import set_language

    set_language("en")

    def boom():
        raise RuntimeError("kaputt")

    tray = undo_tray(undo_last_output=boom)
    tray._run_undo()
    tray._ui_queue.get()()
    assert "kaputt" in tray._toast.shown[0][0] and tray._toast.shown[0][1] is False
