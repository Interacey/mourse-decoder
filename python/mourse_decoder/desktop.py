"""Title of the window in the foreground, for ``{WaitWindow …}`` in macros."""

from __future__ import annotations

import ctypes
import sys


def foreground_title() -> str:
    if sys.platform != "win32":
        return ""  # shortcut: Windows only, so {WaitWindow} times out elsewhere; port when needed
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 512)
    return buf.value
