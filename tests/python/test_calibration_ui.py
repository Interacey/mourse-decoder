"""Calibration page against a fake engine (skipped without a display, like the other panel tests)."""

from __future__ import annotations

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


def test_a_refused_result_is_reported_and_nothing_changes(make_panel):
    page = make_panel()
    page._toggle()
    page._next_step()
    page._next_step()
    feed(page, [("gap_ms", [1, 2200, 2200, 2200, 2200, 2200])])  # letter pause 1650 > unchanged word pause 1400
    page._apply()
    assert page._notes.get().startswith("Not applied:") and "Wortpause" in page._notes.get()
    assert page.controller.settings.letter_gap_ms == 600


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
