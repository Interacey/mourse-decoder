"""While a backup import runs, nothing else may save the config files.

The import writes ``settings.json``, ``function_keys.json`` and the user folders from a worker thread and then loads
the result into the running program. A change made in between (tray menu, a shortcut, a page) would be written over
the import or over the old state, whichever comes last. Every place that saves asks ``gate.check()`` first (or
``gate.blocked()`` where it can simply wait); the import sets the flag from the moment it starts writing until the
program has taken over the new files.

The gate is a flag, not a lock: the Tk thread must never wait for the import, so a change is refused with
``ImportBusy`` (the caller shows the message) and the shortcut polls of the tray are skipped, which leaves the key
presses pending until the import is done.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .i18n import tr


class ImportBusy(RuntimeError):
    """A change was refused because an import is running; nothing was changed."""


class ImportGate:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._running = False
        self._allowed = 0
        self._holders: set[int] = set()

    def begin(self) -> bool:
        """True if the caller may start an import (none is running); it must call ``end`` in every case."""
        with self._guard:
            if self._running:
                return False
            self._running = True
            return True

    def end(self) -> None:
        with self._guard:
            self._running = False
            self._allowed = 0

    def reset(self) -> None:
        """Everything off, other pages' holds included (tests; the program never needs it)."""
        with self._guard:
            self._running = False
            self._allowed = 0
            self._holders.clear()

    def hold(self, owner: object, on: bool) -> None:
        """The page ``owner`` is in the middle of a backup job (checking, asking, writing): Quit has to wait.

        This does not refuse saves (``blocked``): until the import really writes, nothing is half done.
        """
        with self._guard:
            (self._holders.add if on else self._holders.discard)(id(owner))

    def busy(self) -> bool:
        """True from the moment a backup page starts a job until its last step is done (Quit waits for it)."""
        with self._guard:
            return self._running or bool(self._holders)

    @contextmanager
    def allow(self) -> Iterator[None]:
        """Let the saves of the import itself (loading the new state into the program) through."""
        with self._guard:
            self._allowed += 1
        try:
            yield
        finally:
            with self._guard:
                self._allowed = max(0, self._allowed - 1)

    def blocked(self) -> bool:
        with self._guard:
            return self._running and not self._allowed

    def loading(self) -> bool:
        """True while the import loads its new files into the program (inside ``allow``)."""
        with self._guard:
            return self._running and self._allowed > 0

    def check(self) -> None:
        """Raise ``ImportBusy`` while a change must not be saved."""
        if self.blocked():
            raise ImportBusy(tr("backup.err.busy"))


gate = ImportGate()
