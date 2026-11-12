"""Calibration page against a fake engine (skipped without a display, like the other panel tests)."""

from __future__ import annotations

import itertools

import pytest
from mourse_decoder import calibration as cal
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.i18n import set_language
from mourse_decoder.settings import Settings

DOTS = [110, 120, 130, 115, 125, 118]
DASHES = [380, 400, 420, 390, 410, 405]
LETTER_GAPS = [800, 850, 900, 820, 880]
WORD_GAPS = [2000, 2200, 2100, 2300]


@pytest.fixture
def make_panel(tk_root, fake_engine, injector):
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.calibration_panel import CalibrationPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    created = []

    def make(**settings_changes):
        controller = MorseController(fake_engine, injector, DictionaryManager(), Settings().replace(**settings_changes))
        controller.start()
        applied = []

        def apply(new):
            controller.apply_settings(new)
            applied.append(new)

        page = CalibrationPanel(tk_root, controller, apply)
        page.pack()
        tk_root.update()
        page.controller, page.applied = controller, applied
        created.append(page)
        return page

    yield make
    for page in created:
        page.destroy()


def feed(page, session_values):
    for kind, values in session_values:
        for ms in values:
            page.add_timing(kind, ms)


def full_run(page):
    page._toggle()
    feed(page, [("press_ms", DOTS), ("press_ms", DASHES), ("gap_ms", [1] + LETTER_GAPS), ("gap_ms", [1] + WORD_GAPS)])


def test_the_page_starts_idle_with_the_current_values(make_panel):
    page = make_panel()
    assert not page.measuring and not page.controller.calibrating
    assert "Press Start" in page._state.get()
    assert page._results["threshold"].get() == "now 200 ms, suggested no suggestion"
    assert page._progress["dot"].get() == "0 / 6"


def test_start_pauses_typing_and_stop_resumes_it(make_panel, fake_engine, injector):
    page = make_panel()
    page._toggle()
    assert page.measuring and page.controller.calibrating
    fake_engine.letters("...")
    assert injector.calls == []
    page._toggle()
    assert not page.measuring and not page.controller.calibrating
    fake_engine.letters("...")
    assert injector.buffer == "S"


def test_timings_are_ignored_until_start(make_panel):
    page = make_panel()
    page.add_timing("press_ms", 100)
    assert page._progress["dot"].get() == "0 / 6"


def test_a_full_run_shows_a_suggestion_and_stops_measuring(make_panel):
    page = make_panel()
    full_run(page)
    expected = cal.propose(DOTS, DASHES, LETTER_GAPS, WORD_GAPS)
    assert not page.measuring and not page.controller.calibrating
    assert "All steps are done" in page._state.get()
    assert page._results["threshold"].get() == f"now 200 ms, suggested {expected.dot_threshold_ms} ms"
    assert page._results["letter"].get() == f"now 600 ms, suggested {expected.letter_gap_ms} ms"
    assert page._results["word"].get() == f"now 1400 ms, suggested {expected.word_gap_ms} ms"
    assert page._progress["dot"].get() == f"6 / 6 · {expected.dot.median} ms"
    assert page.applied == []  # nothing changes by itself
    assert page.controller.settings.dot_threshold_ms == 200


def test_apply_writes_the_suggested_values(make_panel, fake_engine):
    page = make_panel()
    full_run(page)
    expected = cal.propose(DOTS, DASHES, LETTER_GAPS, WORD_GAPS)
    page._apply()
    s = page.controller.settings
    assert (s.dot_threshold_ms, s.letter_gap_ms, s.word_gap_ms) == (
        expected.dot_threshold_ms,
        expected.letter_gap_ms,
        expected.word_gap_ms,
    )
    assert page._notes.get() == "Values applied."
    assert fake_engine.configure_calls[-1] == (s.dot_threshold_ms, s.letter_gap_ms, s.word_gap_ms)
    assert page._results["threshold"].get().startswith(f"now {s.dot_threshold_ms} ms")


def test_apply_without_a_suggestion_says_so(make_panel):
    page = make_panel()
    page._apply()
    assert page._notes.get() == "There is nothing to apply yet."
    assert page.applied == []


def test_a_letter_pause_that_reaches_the_word_pause_takes_the_word_pause_along(make_panel):
    page = make_panel()
    page._toggle()
    page._next_step()
    page._next_step()
    feed(page, [("gap_ms", [1, 2200, 2200, 2200, 2200, 2200])])  # letter pause 1650 > unchanged word pause 1400
    assert page._results["word"].get() == "now 1400 ms, suggested 2475 ms"  # shown before applying
    assert "raised to 2475 ms as well" in page._notes.get()
    page._apply()
    s = page.controller.settings
    assert (s.letter_gap_ms, s.word_gap_ms) == (1650, 2475)


def test_too_few_or_overlapping_samples_show_the_reason(make_panel):
    page = make_panel()
    page._toggle()
    feed(page, [("press_ms", [100, 110, 105, 108, 102, 107]), ("press_ms", [120, 130, 125, 128, 122, 127])])
    assert "not distinguishable" in page._notes.get()
    assert page._results["threshold"].get().endswith("no suggestion")
    feed(page, [("gap_ms", [1, 800, 850])])
    page._next_step()
    assert "Too few letter pauses" in page._notes.get()


def test_outliers_are_reported(make_panel):
    page = make_panel()
    page._toggle()
    feed(page, [("press_ms", [110, 120, 130, 115, 125, 900])])
    assert "left out as outliers" in page._notes.get()


def test_next_and_restart_buttons_work_on_the_current_step(make_panel):
    page = make_panel()
    page._toggle()
    page.add_timing("press_ms", 100)
    page._restart_step()
    assert page._progress["dot"].get() == "0 / 6"
    page._next_step()
    assert "Dashes" in page._state.get()


def test_leaving_the_page_stops_measuring(make_panel):
    page = make_panel()
    page._toggle()
    page.stop()
    assert not page.controller.calibrating and page._start_btn._text == "Start"


def test_destroying_the_page_releases_the_engine(make_panel, fake_engine, injector):
    page = make_panel()
    controller = page.controller
    page._toggle()
    page.destroy()
    assert not controller.calibrating
    fake_engine.letters("...")
    assert injector.buffer == "S"


def test_a_separate_dash_key_measures_only_pauses(make_panel):
    page = make_panel(separate_dash_key=True)
    assert page._progress["dot"].get() == "–" and page._progress["dash"].get() == "–"
    assert "only the pauses" in page._hint.get()
    page._toggle()
    assert "Pause between letters" in page._state.get()
    page.controller.apply_settings(page.controller.settings.replace(separate_dash_key=False))
    page.stop()
    page.refresh()
    assert page._progress["dot"].get() == "0 / 6"


def test_the_calibration_tab_is_registered_with_a_title_in_every_language():
    import json
    from pathlib import Path

    from mourse_decoder.ui.main_window import MainWindow

    assert "calibration" in MainWindow.TABS
    locales = Path(__file__).resolve().parents[2] / "python" / "mourse_decoder" / "locales"
    english = {k for k in json.loads((locales / "en.json").read_text(encoding="utf-8"))["strings"] if k.startswith("calibration.")}
    english.add("nav.calibration")
    for path in locales.glob("*.json"):
        strings = json.loads(path.read_text(encoding="utf-8"))["strings"]
        assert english <= set(strings), path.name


# --- review fixes: lifecycle of the measuring state, hints, localized messages ----------------------


class ForwardEnd:
    """What TrayApp does: the controller's end of measuring reaches the page."""

    def __init__(self, page) -> None:
        self.page = page

    def on_calibration_ended(self, reason: str) -> None:
        self.page.controller_ended(reason)


def test_controller_stop_resets_the_page(make_panel, fake_engine, injector):
    page = make_panel()
    page.controller.set_listener(ForwardEnd(page))
    page._toggle()
    assert page.measuring
    page.controller.stop()
    assert not page.measuring and not page.controller.calibrating
    assert "Press Start" in page._state.get() and page._start_btn._text == "Start"
    page.controller.start()
    fake_engine.letters("...")
    assert injector.buffer == "S"


def test_the_time_limit_resets_the_page_and_says_why(make_panel, fake_engine, injector):
    page = make_panel()
    page.controller.set_listener(ForwardEnd(page))
    now = [0.0]
    page.controller._clock = lambda: now[0]
    page._toggle()
    now[0] += page.controller.CALIBRATION_MAX_S
    assert page.controller.check_calibration_timeout()
    assert not page.measuring and "10 minutes" in page._notes.get()
    fake_engine.letters("...")
    assert injector.buffer == "S"


def test_hiding_the_page_stops_measuring(make_panel, fake_engine, injector):
    page = make_panel()
    page._toggle()
    page.event_generate("<Unmap>")
    page.update()
    assert not page.measuring and not page.controller.calibrating
    fake_engine.letters("...")
    assert injector.buffer == "S"


def test_an_exception_in_the_page_does_not_leave_the_engine_measuring(make_panel, monkeypatch):
    page = make_panel()
    page._toggle()

    def boom(ms):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(page._session, "add_press", boom)
    page.add_timing("press_ms", 100)
    assert not page.measuring and not page.controller.calibrating


def test_a_failing_controller_does_not_break_stop(make_panel, monkeypatch):
    page = make_panel()
    page._toggle()

    def boom(on):
        raise RuntimeError("engine gone")

    monkeypatch.setattr(page.controller, "set_calibrating", boom)
    page.stop()  # logs, does not raise


def test_a_key_mode_change_while_measuring_stops_measuring(make_panel):
    page = make_panel(separate_dash_key=True)
    page._toggle()
    assert page.measuring and page._session.phases == (cal.LETTER, cal.WORD)
    page.controller.apply_settings(page.controller.settings.replace(separate_dash_key=False))
    page.refresh()
    assert not page.measuring and not page.controller.calibrating
    assert page._session.phases == cal.PHASES


def test_applying_stops_measuring_first(make_panel):
    page = make_panel()
    full_run(page)
    page._toggle()  # a second run
    assert page.measuring
    page._apply()
    assert not page.measuring and not page.controller.calibrating


def test_start_on_a_stopped_controller_says_so(make_panel):
    page = make_panel()
    page.controller.stop()
    page._toggle()
    assert not page.measuring and "not running" in page._notes.get()


def test_a_disabled_morse_input_still_measures_and_says_so(make_panel, fake_engine, injector):
    page = make_panel(enabled=False)
    assert "switched off" in page._notes.get()
    page._toggle()
    assert page.measuring
    page.add_timing("press_ms", 110)
    assert page._progress["dot"].get() == "1 / 6"
    fake_engine.letters("...")
    assert injector.buffer == ""


def test_the_dot_step_shows_no_shortage_of_dashes(make_panel):
    page = make_panel()
    page._toggle()
    feed(page, [("press_ms", DOTS[:2])])
    assert "dashes" not in page._notes.get()
    assert "Too few dots" in page._notes.get()
    assert "one right after the other" in page._hint.get()
    feed(page, [("press_ms", DOTS[2:])])
    assert page._notes.get() == ""  # dashes are not reached yet, nothing is missing


def test_first_pause_after_a_start_counts_in_a_separate_key_session(make_panel):
    page = make_panel(separate_dash_key=True)
    page._toggle()
    page.add_timing("gap_ms", 800)  # press 1 brings no gap event, so this is the first real pause
    assert page._progress["letter"].get().startswith("1 / 5")


def test_a_resumed_measurement_does_not_lose_its_first_pause(make_panel):
    page = make_panel(separate_dash_key=True)
    page._toggle()
    page.add_timing("gap_ms", 800)
    page.stop()
    page._toggle()  # the engine is reset again
    page.add_timing("gap_ms", 820)
    assert page._progress["letter"].get().startswith("2 / 5")


def test_a_switched_off_word_pause_is_named_before_it_is_turned_on(make_panel):
    page = make_panel(word_gap_ms=0)
    full_run(page)
    assert "switched off" in page._notes.get() and "turns it on" in page._notes.get()
    page._apply()
    assert page.controller.settings.word_gap_ms > 0


def test_a_switched_off_word_pause_stays_off_without_the_word_step(make_panel):
    page = make_panel(word_gap_ms=0)
    page._toggle()
    page._next_step()
    page._next_step()
    feed(page, [("gap_ms", [1] + LETTER_GAPS)])
    page._apply()
    assert page.controller.settings.word_gap_ms == 0 and page.controller.settings.letter_gap_ms > 0


def test_a_clamped_value_is_explained_in_the_notes(make_panel):
    page = make_panel()
    page._toggle()
    page._next_step()
    page._next_step()
    feed(page, [("gap_ms", [1, 9000, 9500, 9200, 9400, 9300])])
    assert "outside the allowed range of 100 to 5000 ms" in page._notes.get()
    assert page._results["letter"].get() == "now 600 ms, suggested 5000 ms"


def test_bounce_is_counted_in_the_notes(make_panel):
    page = make_panel()
    page._toggle()
    feed(page, [("press_ms", [5, 8, 12, 110])])
    assert "ignored as contact bounce" in page._notes.get()


def test_a_refused_result_is_localized_without_the_german_validation_text(make_panel):
    set_language("es")
    try:
        page = make_panel()
        page._toggle()
        page._next_step()
        page._next_step()
        feed(page, [("gap_ms", [1, 2200, 2200, 2200, 2200, 2200])])
        text = page._notes.get()
        assert "1650" in text and "1400" in text and "2475" in text and "pausa de palabra" in text
    finally:
        set_language("en")


def test_any_other_refusal_shows_the_localized_sentence_not_the_exception_text(make_panel):
    page = make_panel()
    full_run(page)
    page._apply_settings = lambda new: (_ for _ in ()).throw(ValueError("Die Funktionstaste ..."))
    page._apply()
    assert page._notes.get() == "Not applied: the settings were refused. Check the settings page."


def test_main_window_stops_measuring_on_unmap_hide_and_controller_end():
    try:
        from mourse_decoder.ui import main_window as main_mod
    except Exception as exc:  # pystray needs an X display on Linux
        pytest.skip(f"UI modules not importable here: {exc!r}")
    from types import SimpleNamespace

    calls: list[str] = []
    page = SimpleNamespace(
        stop=lambda: calls.append("stop"), controller_ended=lambda reason: calls.append(f"ended:{reason}")
    )
    root = SimpleNamespace(withdraw=lambda: calls.append("withdraw"))
    window = object.__new__(main_mod.MainWindow)
    window._root, window._pages = root, {"calibration": page}
    window._on_unmap(SimpleNamespace(widget=SimpleNamespace()))  # an event of a child: ignored
    assert calls == []
    window._on_unmap(SimpleNamespace(widget=root))  # minimized or withdrawn
    window.hide()
    window.calibration_ended("timeout")
    assert calls == ["stop", "stop", "withdraw", "ended:timeout"]
    window._pages = {}  # page not opened yet: nothing to do
    window._on_unmap(SimpleNamespace(widget=root))
    window.calibration_ended("stopped")


def test_the_tray_forwards_the_end_and_polls_the_time_limit():
    try:
        from mourse_decoder.ui import tray as tray_mod
    except Exception as exc:  # pystray needs an X display on Linux
        pytest.skip(f"UI modules not importable here: {exc!r}")
    import queue
    from types import SimpleNamespace

    ended: list[str] = []
    checks: list[int] = []
    tray = object.__new__(tray_mod.TrayApp)
    tray._ui_queue = queue.Queue()
    tray._window = SimpleNamespace(calibration_ended=ended.append)
    tray._controller = SimpleNamespace(check_calibration_timeout=lambda: checks.append(1))
    tray.on_calibration_ended("timeout")
    tray._ui_queue.get_nowait()()
    assert ended == ["timeout"]
    tray._poll_calibration()
    assert checks == [1]


# --- second review round: one truth for page and controller -----------------------------------------


def test_a_late_timeout_notice_does_not_touch_a_measurement_started_after_it(make_panel, fake_engine, injector):
    page = make_panel()  # no listener: the notice stays in "the UI queue" until the test delivers it
    page._toggle()
    page.controller._calibration_started -= page.controller.CALIBRATION_MAX_S + 1
    assert page.controller.check_calibration_timeout()
    page._toggle()  # the button still said "Stop" (the notice waits in the UI queue): this stops, nothing more
    assert not page.measuring
    page._toggle()  # a new measurement
    assert page.measuring and page.controller.calibrating
    page.controller_ended("timeout")  # the old notice arrives now
    assert page.measuring and page.controller.calibrating  # page and controller still agree
    fake_engine.letters("...")
    assert injector.buffer == ""
    page.stop()
    assert not page.controller.calibrating
    fake_engine.letters("...")
    assert injector.buffer == "S"


def test_a_stale_page_flag_cannot_keep_the_controller_measuring(make_panel, fake_engine, injector):
    page = make_panel()
    page._toggle()
    page.controller.set_calibrating(False)  # the controller ended it, the page was not told
    page.controller.set_calibrating(True)  # and something started it again
    page.stop()  # leaving the tab has to switch it off, whatever the page believed
    assert not page.controller.calibrating
    fake_engine.letters("...")
    assert injector.buffer == "S"


@pytest.mark.parametrize("ops", list(itertools.product(("start", "stop", "unmap", "late_notice", "timeout"), repeat=3)))
def test_measuring_can_never_stay_on_after_stop_unmap_or_destroy(make_panel, fake_engine, injector, ops):
    page = make_panel()
    page.controller.set_listener(ForwardEnd(page))
    for op in ops:
        if op == "start":
            page._toggle()
        elif op == "stop":
            page.stop()
        elif op == "unmap":
            page.event_generate("<Unmap>")
            page.update()
        elif op == "late_notice":
            page.controller_ended("timeout")
        else:
            page.controller._calibration_started -= page.controller.CALIBRATION_MAX_S + 1
            page.controller.check_calibration_timeout()
        assert page.measuring == page.controller.calibrating  # one truth
    page.stop()
    assert not page.controller.calibrating and not page.measuring
    fake_engine.letters("...")
    assert injector.buffer == "S"


@pytest.mark.parametrize("ops", [("start",), ("start", "stop", "start"), ("start", "timeout", "start")])
def test_destroying_the_page_ends_measuring_in_every_state(make_panel, fake_engine, injector, ops):
    page = make_panel()
    controller = page.controller
    for op in ops:
        if op == "timeout":
            controller._calibration_started -= controller.CALIBRATION_MAX_S + 1
            controller.check_calibration_timeout()
        elif op == "stop":
            page.stop()
        else:
            page._toggle()
    page.destroy()
    assert not controller.calibrating
    fake_engine.letters("...")
    assert injector.buffer == "S"


def test_the_button_text_follows_the_controller(make_panel):
    page = make_panel()
    page._toggle()
    assert page._start_btn._text == "Stop"
    page.controller.set_calibrating(False)
    page.controller_ended("stopped")
    assert page._start_btn._text == "Start" and not page.measuring
