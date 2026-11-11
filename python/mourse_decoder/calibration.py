"""Calibration logic: from measured press times and pauses to suggested timing settings.

No tkinter and no engine in here. The engine reports how long the key was held ("press_ms") and the
silence before a press ("gap_ms"); the calibration page feeds those numbers into a ``Session`` and shows
the ``Proposal`` that ``propose`` computes. Nothing is applied automatically.

Formulas (all in ms, results rounded to whole numbers and clamped to the ranges of the settings page; a
clamped value is reported with a ``clamped_*`` hint):

* Plausibility: a press shorter than ``MIN_PRESS_MS`` (20) or a pause shorter than ``MIN_GAP_MS`` (20) is
  contact bounce and ignored (counted in ``Session.ignored``). Presses above 5000, pauses above 30000 and
  letter pauses above 10000 / word pauses above 20000 are breaks, not measurements.
* Outliers: a sample is dropped if it is farther from the median than
  ``max(2 * 1.4826 * MAD, 0.25 * median)``. The floor keeps identical samples (MAD = 0) from making
  every slightly different one an outlier.
* dot/dash threshold = geometric mean of the dot median and the dash median. Needs
  ``dash median >= 1.5 * dot median``, else the two are "not distinguishable". The result has to lie
  strictly between the two medians, else there is no suggestion (``threshold_unusable``).
* letter pause = ``0.75 * median`` of the letter pauses. A threshold has to lie below the pauses the
  user really makes, otherwise about half of the letters would be cut in two. It also has to be longer than
  ``1.5 *`` the longest pause between two symbols of one letter (the pauses measured while keying the dots
  and dashes, after outlier rejection, at least 3 of them); if it is not, it is raised to just above that
  (``letter_raised``), and if that would reach the user's own letter pauses there is no suggestion
  (``letter_too_close``).
* word pause = geometric mean of the letter median and the word median. Needs
  ``word median >= 1.5 * letter median``. It can only come from samples of the word step, so a word
  pause that is switched off (0) is never turned on without that step; ``word_was_off`` says so.
* every value needs at least 3 valid samples after outlier rejection.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

from .settings import DOT_THRESHOLD_RANGE, LETTER_GAP_RANGE, WORD_GAP_RANGE, Settings

MIN_SAMPLES = 3
MAD_FACTOR = 2.0
MAD_SCALE = 1.4826  # makes the MAD comparable to a standard deviation for normal data
OUTLIER_FLOOR = 0.25  # share of the median that is always accepted
MIN_RATIO = 1.5  # the longer group must have a median at least this much longer than the shorter one
LETTER_FACTOR = 0.75
INNER_FACTOR = 1.5  # the letter pause must be this much longer than the longest pause inside a letter

DOT, DASH, LETTER, WORD = "dot", "dash", "letter", "word"
PHASES = (DOT, DASH, LETTER, WORD)
TARGETS = {DOT: 6, DASH: 6, LETTER: 5, WORD: 4}  # presses asked for in each step
# a sample beyond these is a forgotten key or a break, not a measurement
MAX_PRESS_MS = 5000
MAX_GAP_MS = 30000
MAX_SAMPLE_MS = {LETTER: 10000, WORD: 20000}
# below these it is contact bounce, not a press or a pause
MIN_PRESS_MS = 20
MIN_GAP_MS = 20

# issue codes; the page shows ``calibration.issue.<code>``
TOO_FEW_DOT = "too_few_dot"
TOO_FEW_DASH = "too_few_dash"
OVERLAP = "dots_dashes_overlap"
TOO_FEW_LETTER = "too_few_letter"
TOO_FEW_WORD = "too_few_word"
WORDS_NOT_LONGER = "words_not_longer"
WORD_NEEDS_LETTER = "word_needs_letter"
CLAMPED_THRESHOLD = "clamped_threshold"
CLAMPED_LETTER = "clamped_letter"
CLAMPED_WORD = "clamped_word"
THRESHOLD_UNUSABLE = "threshold_unusable"
LETTER_RAISED = "letter_raised"
LETTER_TOO_CLOSE = "letter_too_close"
WORD_WAS_OFF = "word_was_off"


def reject_outliers(samples: Sequence[int | float]) -> list[int | float]:
    """The samples within the accepted distance of the median, in their original order."""
    if len(samples) < 3:
        return list(samples)
    median = statistics.median(samples)
    mad = statistics.median(abs(s - median) for s in samples)
    limit = max(MAD_FACTOR * MAD_SCALE * mad, OUTLIER_FLOOR * median)
    return [s for s in samples if abs(s - median) <= limit]


def _clamp(value: float, bounds: tuple[int, int]) -> int:
    low, high = bounds
    return max(low, min(high, round(value)))


@dataclass(frozen=True)
class Group:
    """One kind of sample after outlier rejection."""

    median: int | None  # None if fewer than MIN_SAMPLES valid samples are left
    valid: int
    dropped: int
    high: int | None = None  # the largest valid sample, None like the median


def summarize(samples: Sequence[int | float]) -> Group:
    kept = reject_outliers(samples)
    dropped = len(samples) - len(kept)
    if len(kept) < MIN_SAMPLES:
        return Group(None, len(kept), dropped)
    return Group(round(statistics.median(kept)), len(kept), dropped, round(max(kept)))


@dataclass(frozen=True)
class Proposal:
    """Suggested values; ``None`` means "no suggestion", the reasons are in ``issues``.

    ``details[code]`` holds the numbers the text of an issue needs (``wanted``, ``low``, ``high`` ...).
    """

    dot_threshold_ms: int | None = None
    letter_gap_ms: int | None = None
    word_gap_ms: int | None = None
    dot: Group = field(default_factory=lambda: Group(None, 0, 0))
    dash: Group = field(default_factory=lambda: Group(None, 0, 0))
    letter: Group = field(default_factory=lambda: Group(None, 0, 0))
    word: Group = field(default_factory=lambda: Group(None, 0, 0))
    issues: tuple[str, ...] = ()
    details: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """At least one value can be applied."""
        return any(v is not None for v in (self.dot_threshold_ms, self.letter_gap_ms, self.word_gap_ms))


def propose(
    dots: Sequence[int | float],
    dashes: Sequence[int | float],
    letters: Sequence[int | float],
    words: Sequence[int | float],
    *,
    inner: Sequence[int | float] = (),
    steps: Sequence[str] | None = None,
    current: str | None = None,
    word_was_off: bool = False,
) -> Proposal:
    """Suggested settings for the measured samples (see the module text for the formulas).

    ``inner`` are the pauses between two symbols measured in the dot and dash steps. ``steps`` and
    ``current`` tell which steps the session has (and the one being measured, None when finished): a
    shortage is only reported for the current and the earlier steps, not for steps not reached yet.
    Without ``steps`` a shortage is reported for both steps of a pair as soon as one has samples.
    """
    dot, dash, letter, word = summarize(dots), summarize(dashes), summarize(letters), summarize(words)
    inner_group = summarize(inner)
    issues: list[str] = []
    details: dict[str, dict[str, int]] = {}
    threshold = letter_gap = word_gap = None
    samples = {DOT: dots, DASH: dashes, LETTER: letters, WORD: words}

    def reported(phase: str) -> bool:
        if steps is None:
            pair = (DOT, DASH) if phase in (DOT, DASH) else (LETTER, WORD)
            return any(samples[p] for p in pair)
        if phase not in steps:
            return False
        here = steps.index(current) if current in steps else len(steps)
        index = steps.index(phase)
        return index < here or (index == here and bool(samples[phase]))

    def add(code: str, **numbers: int) -> None:
        issues.append(code)
        if numbers:
            details[code] = numbers

    if reported(DOT) and dot.median is None:
        add(TOO_FEW_DOT)
    if reported(DASH) and dash.median is None:
        add(TOO_FEW_DASH)
    if dot.median is not None and dash.median is not None:
        if dash.median < MIN_RATIO * dot.median:
            add(OVERLAP)
        else:
            wanted = round(math.sqrt(dot.median * dash.median))
            value = _clamp(wanted, DOT_THRESHOLD_RANGE)
            low, high = DOT_THRESHOLD_RANGE
            if not dot.median < value < dash.median:
                add(THRESHOLD_UNUSABLE, wanted=wanted, low=low, high=high, dot=dot.median, dash=dash.median)
            else:
                threshold = value
                if value != wanted:
                    add(CLAMPED_THRESHOLD, wanted=wanted, value=value, low=low, high=high)

    if reported(LETTER) and letter.median is None:
        add(TOO_FEW_LETTER)
    if letter.median is not None:
        wanted = round(LETTER_FACTOR * letter.median)
        value = _clamp(wanted, LETTER_GAP_RANGE)
        low, high = LETTER_GAP_RANGE
        raised = False
        if inner_group.high is not None and value <= INNER_FACTOR * inner_group.high:
            value = _clamp(math.floor(INNER_FACTOR * inner_group.high) + 1, LETTER_GAP_RANGE)
            raised = True
            if value <= INNER_FACTOR * inner_group.high:
                value = None  # the range ends before the pause would be long enough
        if value is not None and value >= letter.median:
            value = None  # it would cut about half of the user's own letter pauses
        if value is None:
            add(LETTER_TOO_CLOSE, inner=inner_group.high or 0, letter=letter.median)
        else:
            letter_gap = value
            if raised:
                add(LETTER_RAISED, inner=inner_group.high, value=value)
            elif value != wanted:
                add(CLAMPED_LETTER, wanted=wanted, value=value, low=low, high=high)

    if reported(WORD):
        if word.median is None:
            add(TOO_FEW_WORD)
    if word.median is not None:
        if letter.median is None:
            add(WORD_NEEDS_LETTER)
        elif word.median < MIN_RATIO * letter.median:
            add(WORDS_NOT_LONGER)
        else:
            wanted = round(math.sqrt(letter.median * word.median))
            word_gap = _clamp(wanted, WORD_GAP_RANGE)
            low, high = WORD_GAP_RANGE
            # the word pause is above the letter median and so above the letter pause (< that median)
            if word_gap != wanted:
                add(CLAMPED_WORD, wanted=wanted, value=word_gap, low=low, high=high)
            if word_was_off:
                add(WORD_WAS_OFF, value=word_gap)

    return Proposal(threshold, letter_gap, word_gap, dot, dash, letter, word, tuple(issues), details)


class ApplyError(ValueError):
    """A proposal cannot be applied; ``code`` and ``numbers`` give the page what it needs for its own text."""

    def __init__(self, code: str, **numbers: int) -> None:
        super().__init__(code)
        self.code = code
        self.numbers = numbers


def apply_proposal(settings: Settings, proposal: Proposal) -> Settings:
    """``settings`` with the suggested values put in.

    ``ApplyError`` if a word pause that was not suggested is then not longer than the new letter pause,
    ValueError (German text of ``Settings.validate``) for anything else invalid; nothing is changed silently.
    """
    changes = {
        name: value
        for name, value in (
            ("dot_threshold_ms", proposal.dot_threshold_ms),
            ("letter_gap_ms", proposal.letter_gap_ms),
            ("word_gap_ms", proposal.word_gap_ms),
        )
        if value is not None
    }
    result = settings.replace(**changes)
    if result.word_gap_ms and result.letter_gap_ms and result.word_gap_ms <= result.letter_gap_ms:
        raise ApplyError("word_not_longer", letter=result.letter_gap_ms, word=result.word_gap_ms)
    if errors := result.validate():
        raise ValueError("; ".join(errors))
    return result


class Session:
    """The guided measurement: dots, then dashes, then letter pauses, then word pauses.

    ``add_press`` and ``add_gap`` take what the engine reported; a number only counts in the step it
    belongs to. The engine is reset whenever measuring starts, so the first press after that brings no
    pause (call ``resume`` then). After a step change the first pause is stale (the user was reading) and
    is skipped, so ``TARGETS[phase]`` pauses need one press more than that. Pauses in the dot and dash
    steps are the pauses inside a letter: they are kept apart and limit the letter pause.
    """

    def __init__(self, use_threshold: bool = True) -> None:
        # with separate dot and dash keys the press time decides nothing, so only the pauses are measured
        self.phases = PHASES if use_threshold else (LETTER, WORD)
        self._samples: dict[str, list[int]] = {p: [] for p in PHASES}
        self._inner: dict[str, list[int]] = {DOT: [], DASH: []}
        self._index = 0
        self._skip_gap = False  # a new session starts right after an engine reset: no stale pause
        self.ignored = 0  # bounce: presses and pauses under 20 ms

    @property
    def phase(self) -> str | None:
        """The current step, None when all steps are done."""
        return self.phases[self._index] if self._index < len(self.phases) else None

    @property
    def done(self) -> bool:
        return self.phase is None

    @property
    def count(self) -> int:
        """Samples collected in the current step."""
        return len(self._samples[self.phase]) if self.phase else 0

    @property
    def target(self) -> int:
        return TARGETS[self.phase] if self.phase else 0

    def samples(self, phase: str) -> list[int]:
        return list(self._samples[phase])

    def resume(self) -> None:
        """Measuring (re)starts after an engine reset: the next press has no pause before it."""
        self._skip_gap = False

    def add_press(self, ms: int) -> bool:
        """Record a press time; True if it was taken."""
        if self.phase not in (DOT, DASH) or ms > MAX_PRESS_MS:
            return False
        if ms < MIN_PRESS_MS:
            self.ignored += 1
            return False
        self._take(ms)
        return True

    def add_gap(self, ms: int) -> bool:
        """Record a pause before a press; True if it counts for the current step."""
        if self.phase is None:
            return False
        if self._skip_gap:
            self._skip_gap = False
            return False
        if ms < MIN_GAP_MS:
            self.ignored += 1
            return False
        if self.phase in (DOT, DASH):
            if ms <= MAX_PRESS_MS:  # a longer one is a break between two tries
                self._inner[self.phase].append(ms)
            return False
        if ms > MAX_SAMPLE_MS[self.phase]:
            return False
        self._take(ms)
        return True

    def next_step(self) -> None:
        """Go on without waiting for the full count (the proposal then says what is missing)."""
        if self._index < len(self.phases):
            self._index += 1
            self._skip_gap = True

    def restart_step(self) -> None:
        """Throw away the samples of the current step."""
        if self.phase:
            self._samples[self.phase].clear()
            self._inner.get(self.phase, []).clear()
            self._skip_gap = True

    def proposal(self, word_was_off: bool = False) -> Proposal:
        inner = self._inner[DOT] + self._inner[DASH]
        return propose(
            *(self._samples[p] for p in PHASES),
            inner=inner,
            steps=self.phases,
            current=self.phase,
            word_was_off=word_was_off,
        )

    def _take(self, ms: int) -> None:
        self._samples[self.phase].append(ms)
        if len(self._samples[self.phase]) >= TARGETS[self.phase]:
            self.next_step()
