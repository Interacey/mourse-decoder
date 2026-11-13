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
    p = cal.propose([20, 20, 20], [40, 40, 40], [110, 110, 110], [300, 300, 300])
    assert p.dot_threshold_ms is None and cal.THRESHOLD_UNUSABLE in p.issues  # 28 would be raised to 50: above both
    assert p.letter_gap_ms == LETTER_GAP_RANGE[0] and cal.CLAMPED_LETTER in p.issues  # 0.75 * 110 = 82
    huge = cal.propose([1500] * 3, [4000] * 3, [20000] * 3, [29000] * 3)
    assert huge.dot_threshold_ms <= DOT_THRESHOLD_RANGE[1]
    assert huge.letter_gap_ms == LETTER_GAP_RANGE[1]
    assert huge.word_gap_ms is None or huge.word_gap_ms <= WORD_GAP_RANGE[1]


def test_a_letter_pause_that_the_range_raises_above_the_real_pauses_is_refused():
    # 0.75 * 50 = 37 would be raised to the minimum of 100: longer than every pause the user makes
    p = cal.propose([], [], [50] * 3, [80] * 3)
    assert p.letter_gap_ms is None
    assert cal.LETTER_BELOW_RANGE in p.issues and cal.LETTER_TOO_CLOSE not in p.issues  # no inner pauses were measured


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


def test_apply_raises_the_unchanged_word_pause_instead_of_refusing_everything():
    # letter pause 1650 would not be shorter than the unchanged word pause 1400: the threshold and the
    # letter pause are applied and the word pause goes to 1.5 times the letter pause
    p = cal.propose(DOTS, DASHES, [2200] * 3, [1000] * 3)
    assert p.letter_gap_ms == 1650 and p.word_gap_ms is None and p.dot_threshold_ms
    new = cal.apply_proposal(Settings(), p)
    assert (new.dot_threshold_ms, new.letter_gap_ms, new.word_gap_ms) == (p.dot_threshold_ms, 1650, 2475)


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



# --- review fixes ---------------------------------------------------------------------------------


def test_separate_key_session_counts_the_first_real_pause():
    # the engine was reset: press 1 has no pause event, press 2 brings the first real pause
    s = cal.Session(use_threshold=False)
    assert s.add_gap(800)
    assert s.samples(cal.LETTER) == [800]


def test_resume_after_a_stop_does_not_swallow_a_pause():
    s = cal.Session(use_threshold=False)
    s.restart_step()  # a stale pause is expected after this ...
    s.resume()  # ... but measuring was restarted with an engine reset, so there is none
    assert s.add_gap(800)


def test_step_change_still_skips_the_stale_pause():
    s = cal.Session()
    for ms in DOTS + DASHES:
        s.add_press(ms)
    assert s.phase == cal.LETTER
    assert not s.add_gap(9000)  # the time the user needed to read the next hint
    assert s.add_gap(800)


@pytest.mark.parametrize(
    ("steps_done", "dots", "dashes", "letters", "words", "expected"),
    [
        # nothing is reported for steps that were not reached yet
        (0, [], [], [], [], ()),
        (0, [100, 110], [], [], [], (cal.TOO_FEW_DOT,)),
        (0, DOTS, [], [], [], ()),
        (1, DOTS, [400], [], [], (cal.TOO_FEW_DASH,)),
        (1, DOTS, DASHES, [], [], ()),
        (2, DOTS, DASHES, [800], [], (cal.TOO_FEW_LETTER,)),
        (2, DOTS, DASHES, LETTERS, [], ()),
        (3, DOTS, DASHES, LETTERS, [2000], (cal.TOO_FEW_WORD,)),
        (3, DOTS, DASHES, LETTERS, WORDS, ()),
    ],
)
def test_shortages_are_only_reported_up_to_the_current_step(steps_done, dots, dashes, letters, words, expected):
    s = cal.Session()
    for _ in range(steps_done):
        s.next_step()
    s._samples.update({cal.DOT: list(dots), cal.DASH: list(dashes), cal.LETTER: list(letters), cal.WORD: list(words)})
    assert s.proposal().issues == expected


def test_a_skipped_earlier_step_is_reported():
    s = cal.Session()
    s.next_step()
    s.next_step()  # dots and dashes skipped, now in the letter step with nothing yet
    assert s.proposal().issues == (cal.TOO_FEW_DOT, cal.TOO_FEW_DASH)


def test_a_finished_session_reports_every_missing_step():
    s = cal.Session()
    for _ in range(4):
        s.next_step()
    assert s.done
    assert s.proposal().issues == (cal.TOO_FEW_DOT, cal.TOO_FEW_DASH, cal.TOO_FEW_LETTER, cal.TOO_FEW_WORD)


def test_separate_key_session_never_reports_dots_or_dashes():
    s = cal.Session(use_threshold=False)
    assert s.proposal().issues == ()
    s.next_step()
    assert s.proposal().issues == (cal.TOO_FEW_LETTER,)


def test_a_clamped_value_says_so_with_the_numbers():
    p = cal.propose([1500] * 3, [4000] * 3, [20000] * 3, [30000] * 3)
    assert {cal.CLAMPED_THRESHOLD, cal.CLAMPED_LETTER, cal.CLAMPED_WORD} <= set(p.issues)
    assert p.details[cal.CLAMPED_LETTER] == {"wanted": 15000, "value": 5000, "low": 100, "high": 5000}
    assert p.details[cal.CLAMPED_THRESHOLD]["wanted"] == 2449 and p.details[cal.CLAMPED_THRESHOLD]["value"] == 2000


def test_a_value_inside_the_range_has_no_clamp_hint():
    assert cal.propose(DOTS, DASHES, LETTERS, WORDS).details == {}


def test_presses_under_20_ms_are_bounce_and_ignored():
    s = cal.Session()
    assert not s.add_press(19) and s.count == 0 and s.ignored == 1
    assert s.add_press(20) and s.count == 1
    assert not s.add_gap(5) and s.ignored == 2


def test_tiny_dots_and_dashes_give_no_threshold_above_both():
    # 20 ms dots and 40 ms dashes: the geometric mean 28 would be raised to 50, above both
    p = cal.propose([20] * 3, [40] * 3, [], [])
    assert p.dot_threshold_ms is None and p.issues == (cal.THRESHOLD_UNUSABLE,)
    assert p.details[cal.THRESHOLD_UNUSABLE]["wanted"] == 28


def test_the_threshold_always_lies_strictly_between_the_medians():
    for dot in (20, 30, 49, 50, 100, 400, 1300):
        for factor in (1.5, 2, 3, 10):
            p = cal.propose([dot] * 3, [round(dot * factor)] * 3, [], [])
            if p.dot_threshold_ms is not None:
                assert dot < p.dot_threshold_ms < round(dot * factor)


def test_implausibly_long_pauses_are_breaks():
    s = cal.Session(use_threshold=False)
    assert not s.add_gap(cal.MAX_SAMPLE_MS[cal.LETTER] + 1)
    assert s.add_gap(cal.MAX_SAMPLE_MS[cal.LETTER])
    for _ in range(cal.TARGETS[cal.LETTER] - 1):
        s.add_gap(900)
    assert s.phase == cal.WORD
    assert not s.add_gap(cal.MAX_SAMPLE_MS[cal.WORD] + 1)
    assert s.add_gap(cal.MAX_SAMPLE_MS[cal.WORD])


def test_pauses_inside_a_letter_are_collected_in_the_dot_and_dash_steps():
    s = cal.Session()
    s.add_press(110)
    s.add_gap(300)
    s.add_press(120)
    s.add_gap(350)
    assert s._inner[cal.DOT] == [300, 350] and s.samples(cal.LETTER) == []
    s.restart_step()
    assert s._inner[cal.DOT] == []


def test_a_letter_pause_is_raised_above_the_longest_pause_inside_a_letter():
    # inner pauses up to 700: 1.5 * 700 = 1050 is above the letter pauses (850): no pause fits
    p = cal.propose([], [], LETTERS, WORDS, inner=[600, 650, 700, 640, 660])
    assert p.letter_gap_ms is None and p.issues == (cal.LETTER_TOO_CLOSE,)
    # inner pauses up to 500: 1.5 * 500 = 750 is above 0.75 * 850 = 637 and below the letter pauses
    p = cal.propose([], [], LETTERS, WORDS, inner=[400, 450, 500, 420])
    assert p.letter_gap_ms == 751 and p.issues == (cal.LETTER_RAISED,)
    assert p.details[cal.LETTER_RAISED] == {"inner": 500, "value": 751}


def test_quick_inner_pauses_leave_the_letter_pause_alone():
    p = cal.propose([], [], LETTERS, WORDS, inner=[150, 180, 200, 170])
    assert p.letter_gap_ms == 638 and p.issues == ()


def test_one_slow_inner_pause_is_an_outlier_and_does_not_raise_the_letter_pause():
    p = cal.propose([], [], LETTERS, WORDS, inner=[150, 180, 200, 170, 160, 3000])
    assert p.letter_gap_ms == 638 and p.issues == ()


def test_too_few_inner_pauses_are_not_used():
    p = cal.propose([], [], LETTERS, WORDS, inner=[700, 700])
    assert p.letter_gap_ms == 638


def test_the_session_hands_its_inner_pauses_to_the_proposal():
    s = cal.Session()
    inner = [None, 400, 450, 500, 420, 430]
    for presses in (DOTS, DASHES):
        for press, gap in zip(presses, inner, strict=True):
            if gap:
                s.add_gap(gap)
            s.add_press(press)
        s.add_gap(9000)  # stale pause at the step change, skipped
    for ms in LETTERS:
        s.add_gap(ms)
    p = s.proposal()
    assert p.letter_gap_ms == 751 and cal.LETTER_RAISED in p.issues


def test_a_word_pause_that_is_switched_off_is_flagged_when_a_measurement_would_turn_it_on():
    p = cal.propose([], [], LETTERS, WORDS, word_was_off=True)
    assert cal.WORD_WAS_OFF in p.issues and p.details[cal.WORD_WAS_OFF]["value"] == p.word_gap_ms
    assert cal.WORD_WAS_OFF not in cal.propose([], [], LETTERS, WORDS).issues
    # without a word step there is nothing that could turn it on
    p = cal.propose([], [], LETTERS, [], word_was_off=True)
    assert p.word_gap_ms is None and cal.WORD_WAS_OFF not in p.issues
    s = cal.Session(use_threshold=False)
    for ms in LETTERS:
        s.add_gap(ms)
    assert s.proposal(word_was_off=True).word_gap_ms is None


def test_apply_with_a_switched_off_word_pause_only_changes_it_with_word_samples():
    off = Settings().replace(word_gap_ms=0)
    letters_only = cal.propose([], [], LETTERS, [])
    assert cal.apply_proposal(off, letters_only).word_gap_ms == 0
    with_words = cal.propose([], [], LETTERS, WORDS, word_was_off=True)
    assert cal.apply_proposal(off, with_words).word_gap_ms == with_words.word_gap_ms


# --- second review round ----------------------------------------------------------------------------


def test_a_slow_keyer_with_a_skipped_word_step_keeps_the_threshold_and_raises_the_word_pause():
    # review case: letter pauses around 2000, default word pause 1400, word step left out
    s = cal.Session()
    for presses in (DOTS, DASHES):
        for ms in presses:
            s.add_press(ms)
    s.add_gap(9000)
    for ms in (2000, 2100, 1900, 2050, 1950):
        s.add_gap(ms)
    p = s.proposal(word_gap_now=1400)
    assert (p.dot_threshold_ms, p.letter_gap_ms) == (219, 1500)
    assert p.word_gap_ms == 2250 and cal.WORD_RAISED in p.issues
    assert p.details[cal.WORD_RAISED] == {"old": 1400, "letter": 1500, "value": 2250}
    new = cal.apply_proposal(Settings(), p)
    assert (new.dot_threshold_ms, new.letter_gap_ms, new.word_gap_ms) == (219, 1500, 2250)


def test_apply_works_without_the_hint_of_propose():
    p = cal.propose(DOTS, DASHES, [2000, 2100, 1900, 2050, 1950], [])  # no word_gap_now given
    assert p.word_gap_ms is None
    new = cal.apply_proposal(Settings(), p)
    assert new.letter_gap_ms == 1500 and new.word_gap_ms == 2250 and new.dot_threshold_ms == 219


def test_a_switched_off_word_pause_is_not_raised():
    p = cal.propose([], [], [2000, 2100, 1900, 2050, 1950], [], word_gap_now=0)
    assert p.word_gap_ms is None and cal.WORD_RAISED not in p.issues
    assert cal.apply_proposal(Settings().replace(word_gap_ms=0), p).word_gap_ms == 0


def test_a_measured_word_pause_is_never_replaced_by_the_raise():
    p = cal.propose([], [], LETTERS, WORDS, word_gap_now=700)
    assert cal.WORD_RAISED not in p.issues and p.word_gap_ms == round(math.sqrt(850 * 2150))


def test_apply_never_produces_invalid_settings_and_keeps_every_valid_value():
    import random

    rng = random.Random(1)
    bases = [
        Settings(),
        Settings(word_gap_ms=0),
        Settings(letter_gap_ms=3000, word_gap_ms=4000),
        Settings(dot_threshold_ms=50, letter_gap_ms=100, word_gap_ms=101),
        Settings(letter_gap_ms=5000, word_gap_ms=15000),
    ]
    conflicts = 0
    for _ in range(1500):
        s = cal.Session(use_threshold=rng.random() < 0.8)
        for _ in range(rng.randint(0, 60)):
            r = rng.random()
            if r < 0.45:
                s.add_press(rng.choice([rng.randint(0, 6000), rng.randint(30, 150), rng.randint(150, 600)]))
            elif r < 0.9:
                s.add_gap(rng.choice([rng.randint(0, 31000), rng.randint(30, 300), rng.randint(200, 3000)]))
            elif r < 0.95:
                s.next_step()
            else:
                s.restart_step()
        for base in bases:
            p = s.proposal(
                word_was_off=base.word_gap_ms == 0, word_gap_now=base.word_gap_ms, letter_gap_now=base.letter_gap_ms
            )
            if not p.usable:
                continue
            new = cal.apply_proposal(base, p)  # never raises
            assert new.validate() == []
            assert DOT_THRESHOLD_RANGE[0] <= new.dot_threshold_ms <= DOT_THRESHOLD_RANGE[1]
            assert LETTER_GAP_RANGE[0] <= new.letter_gap_ms <= LETTER_GAP_RANGE[1]
            assert new.word_gap_ms == 0 or new.word_gap_ms > new.letter_gap_ms
            for name, value in (("dot_threshold_ms", p.dot_threshold_ms), ("letter_gap_ms", p.letter_gap_ms)):
                if value is not None:
                    assert getattr(new, name) == value  # the valid values are all taken
            if p.word_gap_ms is not None:
                assert new.word_gap_ms == p.word_gap_ms  # what the page shows is what is applied
            conflicts += cal.WORD_RAISED in p.issues
    assert conflicts > 50  # the generator does reach the conflict


def test_bimodal_dots_get_an_overlap_hint_with_the_numbers():
    p = cal.propose([60, 60, 60, 300, 300, 300], [400] * 6, [], [])
    assert p.dot_threshold_ms == 268  # still the best single value
    assert p.issues == (cal.THRESHOLD_OVERLAPS,)
    assert p.details[cal.THRESHOLD_OVERLAPS] == {"value": 268, "dot_max": 300, "dash_min": 400}


def test_a_clean_threshold_has_no_overlap_hint():
    assert cal.THRESHOLD_OVERLAPS not in cal.propose(DOTS, DASHES, [], []).issues


def test_dashes_that_dip_below_the_threshold_are_reported_too():
    p = cal.propose([100] * 6, [113, 150, 150, 150, 150, 150], [], [])  # the 113 is kept (25 % floor)
    assert p.dot_threshold_ms == 122 and cal.THRESHOLD_OVERLAPS in p.issues


def test_letter_pauses_that_are_shorter_than_the_suggestion_are_reported():
    p = cal.propose([100] * 6, [400] * 6, [400, 800, 1200, 600, 1000], [])
    assert p.letter_gap_ms == 600
    assert cal.LETTER_PAUSES_SHORTER in p.issues
    assert p.details[cal.LETTER_PAUSES_SHORTER] == {"value": 600, "low": 400}


def test_even_letter_pauses_have_no_hint():
    assert cal.LETTER_PAUSES_SHORTER not in cal.propose([], [], LETTERS, WORDS).issues


def test_tiny_letter_pauses_without_inner_pauses_get_their_own_reason():
    # separate dash key: no inner pauses were measured, so the text must not talk about them
    p = cal.propose([], [], [90, 100, 110, 95, 105], [], steps=("letter", "word"), current=None)
    assert p.letter_gap_ms is None
    assert cal.LETTER_BELOW_RANGE in p.issues and cal.LETTER_TOO_CLOSE not in p.issues
    assert p.details[cal.LETTER_BELOW_RANGE] == {"letter": 100, "low": 100}


def test_inner_pauses_that_are_too_close_keep_the_old_reason():
    p = cal.propose([], [], LETTERS, WORDS, inner=[600, 650, 700, 640, 660])
    assert cal.LETTER_TOO_CLOSE in p.issues and cal.LETTER_BELOW_RANGE not in p.issues


def test_a_measured_word_pause_below_the_letter_pause_that_stays_is_not_suggested():
    p = cal.propose([], [], [35] * 3, [148] * 3, letter_gap_now=600)  # letter pause refused, 600 stays
    assert p.letter_gap_ms is None and p.word_gap_ms is None and cal.WORDS_NOT_LONGER in p.issues
