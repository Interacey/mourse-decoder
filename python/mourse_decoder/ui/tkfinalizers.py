"""Tk objects are freed on the Tk thread, whichever thread the garbage collector runs on.

A destroyed page leaves reference cycles that hold its ``StringVar``, ``PhotoImage`` and ``Font`` objects. The
collector runs on whatever thread allocates next (the backup worker, the pystray thread, the Rust engine thread,
a macro), and the finalizers of these classes then call into Tcl from that thread: Tcl hands the call to the main
thread and waits for the main loop, about a second per object, and ``Variable`` raises "main thread is not in main
loop" when the loop is only polled (``update()``). ``install`` replaces the finalizers: on the Tk thread they do
what Tk does, on any other thread the object is parked, and ``drain`` (called from the Tk thread by the poll
loops) frees the parked ones.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from typing import Any

from PIL import ImageTk

log = logging.getLogger(__name__)

_TK_THREAD = threading.main_thread()  # the program runs the Tk main loop on its main thread
# classes whose ``__del__`` calls into Tcl (tk.PhotoImage and tk.BitmapImage inherit it from tk.Image)
_CLASSES: tuple[type, ...] = (tk.Variable, tk.Image, tkfont.Font, ImageTk.PhotoImage, ImageTk.BitmapImage)
_originals: dict[type, Any] = {}
_parked: queue.SimpleQueue[tuple[Any, Any]] = queue.SimpleQueue()


def _finalizer(original: Any) -> Any:
    def finalize(self: Any) -> None:
        if threading.current_thread() is _TK_THREAD:
            original(self)
        else:
            _parked.put((original, self))  # keeps it alive; Python runs a finalizer only once, ``drain`` calls it

    finalize._parks = True  # type: ignore[attr-defined]
    return finalize


def install() -> None:
    """Idempotent."""
    for cls in _CLASSES:
        current = cls.__dict__.get("__del__")
        if current is None or getattr(current, "_parks", False):
            continue  # not defined here (inherited and patched there), or already installed
        _originals[cls] = current
        cls.__del__ = _finalizer(current)  # type: ignore[attr-defined]


def drain() -> int:
    """Free the objects that other threads parked. Call from the Tk thread; returns how many."""
    count = 0
    while True:
        try:
            original, obj = _parked.get_nowait()
        except queue.Empty:
            return count
        count += 1
        try:
            original(obj)
        except Exception:  # noqa: BLE001 - e.g. the Tcl interpreter is gone already
            log.debug("Tk-Objekt nicht freigegeben", exc_info=True)
