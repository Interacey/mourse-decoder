"""Calibration logic: outliers, proposal, ranges, apply and the step-by-step session (no tkinter, no engine)."""

from __future__ import annotations

import math

import pytest
from mourse_decoder import calibration as cal
from mourse_decoder.settings import DOT_THRESHOLD_RANGE, LETTER_GAP_RANGE, WORD_GAP_RANGE, Settings

DOTS = [110, 120, 130, 115, 125, 118]
DASHES = [380, 400, 420, 390, 410, 405]
LETTERS = [800, 850, 900, 820, 880]
WORDS = [2000, 2200, 2100, 2300]


def test_a_normal_measurement_gives_all_three_values():
    p = cal.propose(DOTS, DASHES, LETTERS, WORDS)
    assert p.dot.median == round((118 + 120) / 2)
    assert p.dash.median == round((400 + 405) / 2)
    assert p.dot_threshold_ms == round(math.sqrt(p.dot.median * p.dash.median))
    assert p.letter.median == 850
    assert p.letter_gap_ms == round(0.75 * 850)
    assert p.word.median == 2150
    assert p.word_gap_ms == round(math.sqrt(850 * 2150))
    assert p.issues == ()
    assert p.usable
    assert p.dot_threshold_ms < p.dash.median
    assert p.letter_gap_ms < p.word_gap_ms


def test_values_are_whole_numbers_and_deterministic():
    first = cal.propose(DOTS, DASHES, LETTERS, WORDS)
    second = cal.propose(list(reversed(DOTS)), DASHES, LETTERS, WORDS)
    assert first == second
    for value in (first.dot_threshold_ms, first.letter_gap_ms, first.word_gap_ms):
        assert type(value) is int


@pytest.mark.parametrize(
    ("dots", "dashes", "code"),
    [([100, 110], DASHES, cal.TOO_FEW_DOT), (DOTS, [400, 410], cal.TOO_FEW_DASH)],
)
def test_too_few_presses_give_no_threshold(dots, dashes, code):
    p = cal.propose(dots, dashes, [], [])
    assert p.dot_threshold_ms is None
    assert code in p.issues
    assert not p.usable


def test_overlapping_clusters_are_not_distinguishable():
    p = cal.propose([100, 110, 120, 105], [140, 150, 160, 145], [], [])  # dash median 147.5 < 1.5 * 107.5
    assert p.dot_threshold_ms is None
    assert p.issues == (cal.OVERLAP,)


def test_the_ratio_limit_is_inclusive():
    p = cal.propose([100, 100, 100], [150, 150, 150], [], [])
    assert p.issues == () and p.dot_threshold_ms == round(math.sqrt(100 * 150))


def test_too_few_pauses_and_words_without_letters():
    p = cal.propose([], [], [800, 900], [2000, 2100, 2200])
    assert p.letter_gap_ms is None and p.word_gap_ms is None
    assert p.issues == (cal.TOO_FEW_LETTER, cal.WORD_NEEDS_LETTER)
    p = cal.propose([], [], LETTERS, [2000])
    assert p.letter_gap_ms is not None and p.word_gap_ms is None
    assert p.issues == (cal.TOO_FEW_WORD,)


def test_word_pauses_must_be_longer_than_letter_pauses():
    p = cal.propose([], [], [800, 800, 800], [1000, 1100, 1000])
    assert p.letter_gap_ms == 600
    assert p.word_gap_ms is None and p.issues == (cal.WORDS_NOT_LONGER,)


def test_nothing_measured_has_no_issue_and_no_values():
    p = cal.propose([], [], [], [])
    assert not p.usable and p.issues == ()


def test_an_outlier_is_dropped_and_does_not_move_the_median():
    p = cal.propose(DOTS + [900], DASHES, [], [])
    assert p.dot.dropped == 1 and p.dot.valid == len(DOTS)
    assert p.dot.median == cal.propose(DOTS, DASHES, [], []).dot.median


def test_identical_samples_are_kept():
    assert cal.reject_outliers([200, 200, 200, 200]) == [200, 200, 200, 200]
    assert cal.reject_outliers([200, 200, 200, 210]) == [200, 200, 200, 210]  # inside the 25 % floor


def test_reject_outliers_keeps_order_and_small_lists():
    assert cal.reject_outliers([5, 1]) == [5, 1]
    assert cal.reject_outliers([100, 5000, 110, 105]) == [100, 110, 105]


def test_outliers_can_leave_too_few_samples():
    p = cal.propose([100, 100, 5000], DASHES, [], [])
    assert cal.TOO_FEW_DOT in p.issues


def test_values_are_clamped_to_the_settings_ranges():
    p = cal.propose([5, 5, 5], [10, 10, 10], [20, 20, 20], [30, 30, 30])
    assert p.dot_threshold_ms == DOT_THRESHOLD_RANGE[0]
    assert p.letter_gap_ms == LETTER_GAP_RANGE[0]
    huge = cal.propose([1500] * 3, [4000] * 3, [20000] * 3, [29000] * 3)
    assert huge.dot_threshold_ms <= DOT_THRESHOLD_RANGE[1]
    assert huge.letter_gap_ms == LETTER_GAP_RANGE[1]
    assert huge.word_gap_ms is None or huge.word_gap_ms <= WORD_GAP_RANGE[1]


def test_a_word_pause_squeezed_by_the_range_is_dropped():
    # letter pause 0.75 * 50 = 37 is raised to the minimum of 100, the word pause sqrt(50 * 80) = 63 is not
    p = cal.propose([], [], [50] * 3, [80] * 3)
    assert p.letter_gap_ms == LETTER_GAP_RANGE[0]
    assert p.word_gap_ms is None and cal.OUT_OF_RANGE in p.issues


def test_a_clamped_word_pause_stays_longer_than_the_letter_pause():
    p = cal.propose([], [], [20000] * 3, [30000] * 3)
    assert (p.letter_gap_ms, p.word_gap_ms) == (LETTER_GAP_RANGE[1], WORD_GAP_RANGE[1])


def test_every_proposal_passes_the_settings_validation():
    p = cal.propose(DOTS, DASHES, LETTERS, WORDS)
    new = cal.apply_proposal(Settings(), p)
    assert (new.dot_threshold_ms, new.letter_gap_ms, new.word_gap_ms) == (
        p.dot_threshold_ms,
        p.letter_gap_ms,
        p.word_gap_ms,
    )
    assert new.validate() == []


def test_apply_only_touches_suggested_values():
    p = cal.propose(DOTS, DASHES, [], [])
    new = cal.apply_proposal(Settings(), p)
    assert new.dot_threshold_ms == p.dot_threshold_ms
    assert (new.letter_gap_ms, new.word_gap_ms) == (600, 1400)


def test_apply_refuses_a_result_that_breaks_the_word_pause_rule():
    # letter pause 1650 would not be shorter than the unchanged word pause 1400
    p = cal.propose([], [], [2200] * 3, [1000] * 3)
    assert p.letter_gap_ms == 1650 and p.word_gap_ms is None
    with pytest.raises(ValueError, match="Wortpause"):
        cal.apply_proposal(Settings(), p)


def test_session_runs_through_all_steps():
    s = cal.Session()
    assert s.phase == cal.DOT and s.target == cal.TARGETS[cal.DOT]
    for ms in DOTS:
        assert s.add_press(ms)
    assert s.phase == cal.DASH and s.count == 0
    assert not s.add_gap(500)  # a pause does not count in a press step
    for ms in DASHES:
        s.add_press(ms)
    assert s.phase == cal.LETTER
    assert not s.add_press(120)  # nor a press in a pause step
    assert not s.add_gap(9000)  # the first one only starts the measurement
    for ms in LETTERS:
        assert s.add_gap(ms)
    assert s.phase == cal.WORD
    assert not s.add_gap(1)  # again the start press
    for ms in WORDS:
        s.add_gap(ms)
    assert s.done and s.phase is None and s.target == 0 and s.count == 0
    assert s.samples(cal.DOT) == DOTS
    assert s.proposal() == cal.propose(DOTS, DASHES, LETTERS, WORDS)
    assert not s.add_press(100) and not s.add_gap(100)


def test_session_ignores_absurd_values():
    s = cal.Session()
    assert not s.add_press(0) and not s.add_press(cal.MAX_PRESS_MS + 1) and not s.add_press(-4)
    assert s.count == 0
    s.next_step()
    s.next_step()
    s.add_gap(1)
    assert not s.add_gap(cal.MAX_GAP_MS + 1) and not s.add_gap(0)


def test_session_next_and_restart():
    s = cal.Session()
    s.add_press(100)
    assert s.count == 1
    s.restart_step()
    assert s.count == 0 and s.phase == cal.DOT
    s.next_step()
    assert s.phase == cal.DASH
    for _ in range(3):
        s.next_step()
    assert s.done
    s.next_step()  # no error past the end
    s.restart_step()


def test_session_with_a_separate_dash_key_measures_only_pauses():
    s = cal.Session(use_threshold=False)
    assert s.phases == (cal.LETTER, cal.WORD) and s.phase == cal.LETTER
    s.add_gap(1)
    for ms in LETTERS:
        s.add_gap(ms)
    for _ in range(1 + cal.TARGETS[cal.WORD]):
        s.add_gap(2100)
    assert s.done
    p = s.proposal()
    assert p.dot_threshold_ms is None and p.letter_gap_ms and p.word_gap_ms and p.issues == ()


def test_restart_in_a_pause_step_skips_the_start_press_again():
    s = cal.Session(use_threshold=False)
    s.add_gap(1)
    s.add_gap(800)
    s.restart_step()
    assert s.count == 0
    assert not s.add_gap(5000)
    assert s.add_gap(800)
