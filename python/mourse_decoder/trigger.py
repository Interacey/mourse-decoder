"""External macro trigger: the protocol, the checks and the service around the named pipe (no Windows code here).

A local caller (a script, a stream deck, an AutoHotkey file) can start a macro that the user wrote and marked as
triggerable. The request names a slot (``F1``..``F100``) and nothing else: the caller can never choose text. The pipe
(``trigger_pipe.py``) only lets the user's own account in; this module decides what a request is worth.

Wire format, one line each way, ASCII, at most ``MAX_REQUEST_BYTES``::

    MOURSE1 TRIGGER F57\\n   ->   OK\\n   or   ERR <code>\\n
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("mourse_decoder")

PROTOCOL = "MOURSE1"
MAX_REQUEST_BYTES = 64
READ_TIMEOUT_S = 2.0
RATE_LIMIT = (10, 10.0)  # requests per window in seconds; rejected ones count too
LOG_NOISE_LIMIT = (20, 10.0)  # refused requests that are logged per window in seconds; the rest is only counted
MAX_SLOT = 100

CODES = ("ok", "bad_request", "unknown_macro", "not_allowed", "inactive", "busy", "rate_limited", "broken")

# Exit codes of the client (``MourseDecoder.exe --trigger F57``). 2 (bad id) comes from the argument check.
EXIT_CODES = {
    "ok": 0,
    "pipe_not_found": 10,
    "access_denied": 11,
    "unknown_macro": 12,
    "not_allowed": 12,
    "busy": 13,
    "rate_limited": 14,
    "inactive": 15,
    "bad_request": 16,
    "broken": 16,
    "protocol": 16,
    "unsupported": 16,  # no Windows pipe API, or the account SID cannot be read
    "timeout": 17,
}

_REQUEST = re.compile(rf"\A{PROTOCOL} TRIGGER F([1-9][0-9]{{0,2}})\n\Z")
_SID = re.compile(r"\AS-1-\d+(-\d+)+\Z")
PIPE_PREFIX = "\\\\.\\pipe\\MourseDecoder-"


class BadRequest(ValueError):
    """The request line is not exactly ``MOURSE1 TRIGGER F<n>\\n``."""


def parse_request(raw: bytes) -> int:
    """The slot number of a request; BadRequest for anything but the one accepted form."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise BadRequest("too long")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise BadRequest("not ASCII") from None
    match = _REQUEST.match(text)
    slot = int(match.group(1)) if match else 0
    if not 1 <= slot <= MAX_SLOT:
        raise BadRequest("not a trigger request")
    return slot


def format_response(code: str) -> bytes:
    if code not in CODES:
        raise ValueError(f"unknown code {code!r}")
    return b"OK\n" if code == "ok" else f"ERR {code}\n".encode("ascii")


def parse_response(raw: bytes) -> str:
    """The code in a reply; anything that is not ``OK`` or ``ERR <known code>`` counts as ``bad_request``."""
    text = raw.decode("ascii", "replace")
    if text == "OK\n":
        return "ok"
    if text.startswith("ERR ") and text.endswith("\n"):
        code = text[4:-1]
        if code in CODES and code != "ok":
            return code
    return "bad_request"


def pipe_name(sid: str) -> str:
    if not _SID.match(sid):
        raise ValueError(f"not a SID: {sid!r}")
    return PIPE_PREFIX + sid


def pipe_sddl(sid: str) -> str:
    """DACL of the pipe: network logons denied, the user's own account allowed, nobody else."""
    if not _SID.match(sid):
        raise ValueError(f"not a SID: {sid!r}")
    return f"D:P(D;;GA;;;NU)(A;;GA;;;{sid})"


class RateLimiter:
    """Sliding window over the last ``limit`` + 1 requests; a rejected request counts like an accepted one."""

    def __init__(self, limit: int = RATE_LIMIT[0], window_s: float = RATE_LIMIT[1], clock=time.monotonic) -> None:
        self._limit = limit
        self._window = window_s
        self._clock = clock
        self._times: deque[float] = deque(maxlen=limit + 1)
        self._guard = threading.Lock()

    def allow(self) -> bool:
        with self._guard:
            now = self._clock()
            self._times.append(now)
            return len(self._times) <= self._limit or now - self._times[0] > self._window


@dataclass(frozen=True)
class TriggerEvent:
    time: float  # epoch seconds
    slot: int | None  # None: the request had no readable slot
    code: str


class TriggerLog:
    """The last requests (for the settings page) and, with ``path``, a text file of them. Never raises on I/O errors.

    Refused requests (any code but ``ok``) are logged at most ``LOG_NOISE_LIMIT`` per window, so a flood of garbage
    cannot push the real history out of the ring or rotate the file. ``add`` still returns the event; the file gets
    one line ``<time> F- suppressed <n>`` when the next window starts. ``ok`` is never suppressed.
    """

    def __init__(
        self, max_events: int = 100, path: Path | None = None, max_bytes: int = 200_000, clock=time.monotonic
    ) -> None:
        self._events: deque[TriggerEvent] = deque(maxlen=max_events)
        self._path = path
        self._max_bytes = max_bytes
        self._clock = clock
        self._guard = threading.Lock()
        self._window_start = clock()
        self._refused = 0  # logged refused requests in this window
        self._suppressed = 0
        self._rotation_failed = False

    def add(self, slot: int | None, code: str, now: float | None = None) -> TriggerEvent:
        event = TriggerEvent(time.time() if now is None else now, slot, code)
        with self._guard:
            if self._clock() - self._window_start >= LOG_NOISE_LIMIT[1]:
                if self._suppressed:
                    self._append(self._line(event.time, None, f"suppressed {self._suppressed}"))
                self._window_start, self._refused, self._suppressed = self._clock(), 0, 0
            if code != "ok":
                if self._refused >= LOG_NOISE_LIMIT[0]:
                    self._suppressed += 1
                    return event
                self._refused += 1
            self._events.append(event)
            self._append(self._line(event.time, slot, code))
        return event

    def events(self) -> list[TriggerEvent]:
        """Oldest first, newest last."""
        with self._guard:
            return list(self._events)

    @staticmethod
    def _line(when: float, slot: int | None, text: str) -> str:
        return f"{datetime.fromtimestamp(when).isoformat(timespec='seconds')} F{slot or '-'} {text}\n"

    def _append(self, line: str) -> None:
        if self._path is None:
            return
        try:
            with open(self._path, "a", encoding="ascii", newline="") as handle:
                handle.write(line)
            if self._path.stat().st_size > self._max_bytes:
                self._rotate()
        except OSError:
            log.warning("Trigger-Protokoll nicht geschrieben: %s", self._path, exc_info=True)

    def _rotate(self) -> None:
        assert self._path is not None
        try:
            os.replace(self._path, self._path.with_name(self._path.name + ".1"))
        except OSError:  # e.g. trigger.log.1 is open elsewhere: keep the file bounded by starting it again
            if not self._rotation_failed:
                self._rotation_failed = True
                log.warning("Trigger-Protokoll nicht rotiert: %s", self._path, exc_info=True)
            with open(self._path, "w", encoding="ascii", newline=""):
                pass


def handle_request(
    raw: bytes,
    *,
    run: Callable[[int], str],
    limiter: RateLimiter,
    log: TriggerLog,
    enabled: Callable[[], bool],
    blocked: Callable[[], bool],
) -> bytes:
    """Answer one request; ``run(slot)`` returns ``"ok"`` or one of ``CODES``. Every request is logged.

    The reply is always built by ``format_response`` from a code, so neither the request nor an exception text
    (which could name a macro) ever reaches the caller.
    """
    slot: int | None = None
    if not limiter.allow():  # before anything else: a flood must not cost more than a counter
        code = "rate_limited"
    elif not enabled():
        code = "inactive"
    else:
        try:
            slot = parse_request(raw)
        except BadRequest:
            code = "bad_request"
        else:
            if blocked():
                code = "busy"
            else:
                try:
                    code = run(slot)
                    if code not in CODES:
                        code = "broken"
                except Exception:  # noqa: BLE001 - nothing of it goes to the caller
                    code = "broken"
    log.add(slot, code)
    return format_response(code)


class _AnnouncingLog:
    """The log of the service: tells ``announce`` about every accepted trigger."""

    def __init__(self, log: TriggerLog, announce: Callable[[TriggerEvent], None] | None) -> None:
        self._log = log
        self._announce = announce

    def add(self, slot: int | None, code: str, now: float | None = None) -> TriggerEvent:
        event = self._log.add(slot, code, now)
        if code == "ok" and self._announce is not None:
            try:
                self._announce(event)
            except Exception:  # noqa: BLE001 - a broken notice must not turn an accepted trigger into an error
                log.exception("Trigger-Hinweis fehlgeschlagen")
        return event


class TriggerService:
    """Starts and stops the pipe server with the setting ``trigger_enabled``.

    ``status``: ``"off"`` (setting off), ``"running"``, ``"unavailable"`` (setting on, but the pipe could not be
    opened: name taken, no SID, not Windows).
    """

    def __init__(
        self,
        controller,
        log: TriggerLog,
        blocked: Callable[[], bool] | None = None,
        announce: Callable[[TriggerEvent], None] | None = None,
        server_factory: Callable[[str, str, Callable[[bytes], bytes]], object] | None = None,
        sid: Callable[[], str] | None = None,
    ) -> None:
        if blocked is None:
            from .import_gate import gate

            blocked = gate.blocked
        self._controller = controller
        self._log = _AnnouncingLog(log, announce)
        self._blocked = blocked
        self._factory = server_factory
        self._sid = sid
        self._limiter = RateLimiter()
        self._guard = threading.Lock()
        self._server = None
        self._wanted = False
        self._failed = False

    @property
    def status(self) -> str:
        with self._guard:
            if not self._wanted:
                return "off"
            return "unavailable" if self._failed else "running"

    def start(self, settings) -> None:
        self.apply(settings)

    def apply(self, settings) -> None:
        """Follow ``settings.trigger_enabled``: open the pipe when on (also after a failure), close it when off."""
        if getattr(settings, "trigger_enabled", False) is True:
            with self._guard:
                self._wanted = True
                if self._server is None:
                    self._failed = not self._open()
        else:
            self.stop()

    def stop(self) -> None:
        with self._guard:
            self._wanted = False
            self._failed = False
            server, self._server = self._server, None
        if server is not None:
            server.stop()

    def _open(self) -> bool:
        factory, sid = self._factory, self._sid
        if factory is None or sid is None:
            if sys.platform != "win32":
                return False
            from . import trigger_pipe

            factory, sid = factory or trigger_pipe.PipeServer, sid or trigger_pipe.current_user_sid
        try:
            identity = sid()
            server = factory(pipe_name(identity), pipe_sddl(identity), self._handle)
            if not server.start():  # type: ignore[attr-defined]
                return False
        except Exception:  # noqa: BLE001 - the trigger is optional: whatever fails here must not stop the program
            log.exception("Trigger-Pipe nicht geöffnet")
            return False
        self._server = server
        return True

    def _handle(self, raw: bytes) -> bytes:
        # ``enabled`` is read before ``run``: a switch-off in between still runs that one macro, like a macro that
        # had started a moment before the switch-off.
        return handle_request(
            raw,
            run=self._controller.trigger_slot,
            limiter=self._limiter,
            log=self._log,  # type: ignore[arg-type]
            enabled=lambda: self._wanted,
            blocked=self._blocked,
        )
