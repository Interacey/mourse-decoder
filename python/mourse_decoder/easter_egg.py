"""Easter egg trigger: click SOS (··· ––– ···) on the app icon.

A short click is a dot, a long one a dash. A long pause starts the input over.
"""

from __future__ import annotations

SOS = "...---..."


class SequenceDetector:
    def __init__(self, target: str = SOS, dot_max: float = 0.30, reset_after: float = 1.6) -> None:
        self._target = target
        self._dot_max = dot_max
        self._reset_after = reset_after
        self._buffer = ""
        self._pressed_at: float | None = None
        self._released_at: float | None = None

    def press(self, now: float) -> None:
        if self._released_at is not None and now - self._released_at > self._reset_after:
            self._buffer = ""
        self._pressed_at = now

    def release(self, now: float) -> bool:
        """True once the full sequence has been entered."""
        if self._pressed_at is None:
            return False
        symbol = "." if now - self._pressed_at < self._dot_max else "-"
        self._pressed_at, self._released_at = None, now
        self._buffer = (self._buffer + symbol)[-len(self._target):]
        if self._buffer == self._target:
            self._buffer = ""
            return True
        return False


def bind_sos(widget, callback, clock=None) -> None:
    """Watch left clicks on ``widget`` and call ``callback()`` when SOS is entered."""
    import time

    now = clock or time.monotonic
    detector = SequenceDetector()
    widget.bind("<ButtonPress-1>", lambda _e: detector.press(now()), add="+")

    def released(_event) -> None:
        if detector.release(now()):
            callback()

    widget.bind("<ButtonRelease-1>", released, add="+")
