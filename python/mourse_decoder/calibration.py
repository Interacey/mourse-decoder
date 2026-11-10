"""Calibration logic: from measured press times and pauses to suggested timing settings.

No tkinter and no engine in here. The engine reports how long the key was held ("press_ms") and the
silence before a press ("gap_ms"); the calibration page feeds those numbers into a ``Session`` and shows
the ``Proposal`` that ``propose`` computes. Nothing is applied automatically.

Formulas (all in ms, results rounded to whole numbers and clamped to the ranges of the settings page):

* Outliers: a sample is dropped if it is farther from the median than
  ``max(2 * 1.4826 * MAD, 0.25 * median)``. The floor keeps identical samples (MAD = 0) from making
  every slightly different one an outlier.
* dot/dash threshold = geometric mean of the dot median and the dash median. Needs
  ``dash median >= 1.5 * dot median``, else the two are "not distinguishable".
* letter pause = ``0.75 * median`` of the letter pauses. A threshold has to lie below the pauses the
  user really makes, otherwise about half of the letters would be cut in two.
* word pause = geometric mean of the letter median and the word median. Needs
  ``word median >= 1.5 * letter median``.
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

DOT, DASH, LETTER, WORD = "dot", "dash", "letter", "word"
PHASES = (DOT, DASH, LETTER, WORD)
TARGETS = {DOT: 6, DASH: 6, LETTER: 5, WORD: 4}  # presses asked for in each step
# a sample beyond these is a forgotten key or a break, not a measurement
MAX_PRESS_MS = 5000
MAX_GAP_MS = 30000

# issue codes; the page shows ``calibration.issue.<code>``
TOO_FEW_DOT = "too_few_dot"
TOO_FEW_DASH = "too_few_dash"
OVERLAP = "dots_dashes_overlap"
TOO_FEW_LETTER = "too_few_letter"
TOO_FEW_WORD = "too_few_word"
WORDS_NOT_LONGER = "words_not_longer"
WORD_NEEDS_LETTER = "word_needs_letter"
OUT_OF_RANGE = "out_of_range"


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


def summarize(samples: Sequence[int | float]) -> Group:
    kept = reject_outliers(samples)
    dropped = len(samples) - len(kept)
    if len(kept) < MIN_SAMPLES:
        return Group(None, len(kept), dropped)
    return Group(round(statistics.median(kept)), len(kept), dropped)


@dataclass(frozen=True)
class Proposal:
    """Suggested values; ``None`` means "no suggestion", the reasons are in ``issues``."""

    dot_threshold_ms: int | None = None
    letter_gap_ms: int | None = None
    word_gap_ms: int | None = None
    dot: Group = field(default_factory=lambda: Group(None, 0, 0))
    dash: Group = field(default_factory=lambda: Group(None, 0, 0))
    letter: Group = field(default_factory=lambda: Group(None, 0, 0))
    word: Group = field(default_factory=lambda: Group(None, 0, 0))
    issues: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """At least one value can be applied."""
        return any(v is not None for v in (self.dot_threshold_ms, self.letter_gap_ms, self.word_gap_ms))


def propose(
    dots: Sequence[int | float],
    dashes: Sequence[int | float],
    letters: Sequence[int | float],
    words: Sequence[int | float],
) -> Proposal:
    """Suggested settings for the measured samples (see the module text for the formulas)."""
    dot, dash, letter, word = summarize(dots), summarize(dashes), summarize(letters), summarize(words)
    issues: list[str] = []
    threshold = letter_gap = word_gap = None

    if dots or dashes:
        if dot.median is None:
            issues.append(TOO_FEW_DOT)
        if dash.median is None:
            issues.append(TOO_FEW_DASH)
        if dot.median is not None and dash.median is not None:
            if dash.median < MIN_RATIO * dot.median:
                issues.append(OVERLAP)
            else:
                threshold = _clamp(math.sqrt(dot.median * dash.median), DOT_THRESHOLD_RANGE)

    if letters or words:
        if letter.median is None:
            issues.append(TOO_FEW_LETTER)
        else:
            letter_gap = _clamp(LETTER_FACTOR * letter.median, LETTER_GAP_RANGE)
        if word.median is None:
            issues.append(TOO_FEW_WORD)
        elif letter.median is None:
            issues.append(WORD_NEEDS_LETTER)
        elif word.median < MIN_RATIO * letter.median:
            issues.append(WORDS_NOT_LONGER)
        else:
            word_gap = _clamp(math.sqrt(letter.median * word.median), WORD_GAP_RANGE)
            if letter_gap is not None and word_gap <= letter_gap:
                word_gap = None  # clamping to the ranges squeezed the two together
                issues.append(OUT_OF_RANGE)

    return Proposal(threshold, letter_gap, word_gap, dot, dash, letter, word, tuple(issues))


def apply_proposal(settings: Settings, proposal: Proposal) -> Settings:
    """``settings`` with the suggested values put in. ValueError (German text) if the result is invalid.

    A word pause that was not suggested stays as it is; if it is then not longer than the new letter pause,
    ``Settings.validate`` refuses the result, so nothing is changed silently.
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
    if errors := result.validate():
        raise ValueError("; ".join(errors))
    return result


class Session:
    """The guided measurement: dots, then dashes, then letter pauses, then word pauses.

    ``add_press`` and ``add_gap`` take what the engine reported; a number only counts in the step it
    belongs to. In a pause step the first press only starts the measurement (the silence before it is
    whatever came before), so ``TARGETS[phase]`` pauses need one press more than that.
    """

    def __init__(self, use_threshold: bool = True) -> None:
        # with separate dot and dash keys the press time decides nothing, so only the pauses are measured
        self.phases = PHASES if use_threshold else (LETTER, WORD)
        self._samples: dict[str, list[int]] = {p: [] for p in PHASES}
        self._index = 0
        self._skip_gap = self.phases[0] in (LETTER, WORD)

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

    def add_press(self, ms: int) -> bool:
        """Record a press time; True if it was taken."""
        if self.phase not in (DOT, DASH) or not 0 < ms <= MAX_PRESS_MS:
            return False
        self._take(ms)
        return True

    def add_gap(self, ms: int) -> bool:
        """Record a pause before a press; True if it was taken."""
        if self.phase not in (LETTER, WORD):
            return False
        if self._skip_gap:
            self._skip_gap = False
            return False
        if not 0 < ms <= MAX_GAP_MS:
            return False
        self._take(ms)
        return True

    def next_step(self) -> None:
        """Go on without waiting for the full count (the proposal then says what is missing)."""
        if self._index < len(self.phases):
            self._index += 1
            self._skip_gap = self.phase in (LETTER, WORD)

    def restart_step(self) -> None:
        """Throw away the samples of the current step."""
        if self.phase:
            self._samples[self.phase].clear()
            self._skip_gap = self.phase in (LETTER, WORD)

    def proposal(self) -> Proposal:
        return propose(*(self._samples[p] for p in PHASES))

    def _take(self, ms: int) -> None:
        self._samples[self.phase].append(ms)
        if len(self._samples[self.phase]) >= TARGETS[self.phase]:
            self.next_step()
