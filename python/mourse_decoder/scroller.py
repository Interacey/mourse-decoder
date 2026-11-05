"""Continuous mouse-wheel scrolling for the macro commands {Scroll Down} and {Scroll Up}.

The first trigger starts scrolling at once; every further trigger in the same direction makes it
faster, up to a cap (notches per second by level):

    level      1  2  3   4   5   6 and more
    notches/s  3  5  8  12  18  25

A trigger in the other direction reverses at level 1. Scrolling ends on {Scroll Stop}, on Esc,
when Morse input is switched off, when the app quits (all reported by ``should_stop``), on a
wheel error, or after MAX_SCROLL_S.

The Scroller runs in its own thread and must not hold the controller's macro lock: letters
(the speed-up and stop codes) have to keep decoding while it scrolls. It takes no other lock
of the app; ``should_stop`` may call ``Scroller.stop`` itself.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

DOWN, UP = 1, -1
NOTCHES_PER_SECOND: tuple[int, ...] = (3, 5, 8, 12, 18, 25)  # level 1..6; more triggers stay at 25
MAX_SCROLL_S = 600.0  # safety cap, like MAX_TOTAL_WAIT_S in macros.py
POLL_S = 0.05  # stop conditions are checked at least this often


def interval_s(level: int) -> float:
    if level < 1:
        raise ValueError(f"level must be 1 or more, not {level}")
    return 1 / NOTCHES_PER_SECOND[min(level, len(NOTCHES_PER_SECOND)) - 1]


class ScrollState:
    """Direction, speed level and next notch time. Never reads a clock; the caller passes ``now``."""

    def __init__(self) -> None:
        self.direction = 0  # 0 = idle, DOWN or UP
        self.level = 0  # 0 when idle
        self.started_at: float | None = None
        self._next: float | None = None

    def request(self, direction: int, now: float) -> None:
        if direction not in (DOWN, UP):
            raise ValueError(f"direction must be DOWN or UP, not {direction}")
        if self.direction == 0:
            self.direction, self.level, self.started_at, self._next = direction, 1, now, now
        elif direction == self.direction:
            self.level = min(self.level + 1, len(NOTCHES_PER_SECOND))
            # apply the new speed at once instead of after the old, longer wait
            self._next = min(self._next, now + interval_s(self.level))
        else:
            # started_at stays: the safety cap counts the whole session
            self.direction, self.level, self._next = direction, 1, now

    def stop(self) -> None:
        self.direction, self.level, self.started_at, self._next = 0, 0, None, None

    def due(self, now: float) -> int:
        """The notches to send now: 0, DOWN or UP. At most one per call, never a burst."""
        if self.direction == 0 or now < self._next:
            return 0
        step = interval_s(self.level)
        self._next += step
        if self._next <= now:  # the thread stalled; do not catch up
            self._next = now + step
        return self.direction

    def next_due(self) -> float | None:
        return self._next


class Scroller:
    def __init__(
        self,
        wheel: Callable[[int], None],
        should_stop: Callable[[], str | None],
        on_error: Callable[[str], None] = lambda message: None,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], object] | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._wheel = wheel
        self._should_stop = should_stop
        self._on_error = on_error
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._wait = wait or self._wake.wait
        self._spawn = spawn or self._spawn_thread
        self._state = ScrollState()
        self._session = 0  # counts starts and stops; a loop of an older session ends

    @staticmethod
    def _spawn_thread(body: Callable[[], None]) -> None:
        threading.Thread(target=body, name="scroller", daemon=True).start()

    @property
    def running(self) -> bool:
        return self._state.direction != 0

    @property
    def level(self) -> int:
        return self._state.level

    @property
    def direction(self) -> int:
        return self._state.direction

    def request(self, direction: int) -> None:
        """Start scrolling, speed it up, or reverse it."""
        with self._lock:
            was_idle = not self.running
            self._state.request(direction, self._clock())
            if was_idle:
                self._session += 1
            session = self._session
        self._wake.set()
        if was_idle:
            self._spawn(lambda: self._run(session))

    def stop(self) -> bool:
        """Stop scrolling; True if it was scrolling."""
        with self._lock:
            was = self.running
            self._state.stop()
            self._session += 1
        self._wake.set()
        return was

    def _stop_session(self, session: int) -> bool:
        """Stop only if ``session`` is still the current one; an old loop must not stop a newer start."""
        with self._lock:
            if self._session != session:
                return False
            self._state.stop()
            self._session += 1
        self._wake.set()
        return True

    def run(self) -> None:
        """Loop body of the current session; returns when it is stopped."""
        with self._lock:
            session = self._session
        self._run(session)

    def _run(self, session: int) -> None:
        while True:
            self._wake.clear()
            with self._lock:
                if self._session != session or not self.running:
                    return
                started_at = self._state.started_at
            # outside the lock: should_stop may call stop() itself
            reason = self._should_stop()
            if reason:
                self._stop_session(session)
                log.info("Scrollen gestoppt: %s", reason)
                return
            now = self._clock()
            if now - started_at >= MAX_SCROLL_S:
                if self._stop_session(session):
                    self._on_error("Scrollen nach 10 Minuten automatisch beendet.")
                return
            with self._lock:
                if self._session != session:
                    return
                notches = self._state.due(now)
                nxt = self._state.next_due()
            if notches:
                try:
                    self._wheel(notches)
                except Exception as exc:
                    if self._stop_session(session):
                        log.warning("Mausrad-Fehler beim Scrollen: %s", exc)
                        self._on_error(f"Scrollen gestoppt: {exc}")
                    return
            timeout = POLL_S if nxt is None else max(0.0, min(POLL_S, nxt - self._clock()))
            self._wait(timeout)
