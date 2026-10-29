"""Windows clipboard via ctypes: put text on it, save and restore everything on it.

Long texts are pasted instead of typed: typing thousands of characters overflows the
target app's input queue and characters go missing (measured in Notepad: 0 of 6319).
"""

from __future__ import annotations

import ctypes
import sys
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
# GDI handles instead of memory blocks; Windows rebuilds CF_BITMAP and friends from CF_DIB
_HANDLE_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
# points to an OLE object of the copying app, restoring it alone would confuse OLE pastes
_SKIPPED_NAMES = {"Ole Private Data"}


def available() -> bool:
    return sys.platform == "win32"


if available():
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # explicit types, the int defaults would cut 64-bit handles in half
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    _user32.EnumClipboardFormats.restype = wintypes.UINT
    _user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalSize.restype = ctypes.c_size_t
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


class _Open:
    """OpenClipboard with retries: another app may hold it for a moment."""

    def __enter__(self) -> None:
        for _ in range(20):
            if _user32.OpenClipboard(None):
                return
            time.sleep(0.01)
        raise OSError("Zwischenablage ist belegt")

    def __exit__(self, *exc: object) -> None:
        _user32.CloseClipboard()


def _name(fmt: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClipboardFormatNameW(fmt, buf, 256)
    return buf.value


def save() -> list[tuple[int, bytes]]:
    """Copy of every format on the clipboard that ``restore`` can put back."""
    saved: list[tuple[int, bytes]] = []
    with _Open():
        fmt = 0
        while fmt := _user32.EnumClipboardFormats(fmt):
            if fmt in _HANDLE_FORMATS or _name(fmt) in _SKIPPED_NAMES:
                continue
            handle = _user32.GetClipboardData(fmt)
            pointer = _kernel32.GlobalLock(handle) if handle else None
            if not pointer:
                continue  # no memory block behind it
            try:
                saved.append((fmt, ctypes.string_at(pointer, _kernel32.GlobalSize(handle))))
            finally:
                _kernel32.GlobalUnlock(handle)
    return saved


def restore(saved: list[tuple[int, bytes]]) -> None:
    with _Open():
        _user32.EmptyClipboard()
        for fmt, data in saved:
            _put(fmt, data)


def set_text(text: str) -> None:
    with _Open():
        _user32.EmptyClipboard()
        _put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))


def get_text() -> str | None:
    with _Open():
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer)
        finally:
            _kernel32.GlobalUnlock(handle)


def _put(fmt: int, data: bytes) -> None:
    """SetClipboardData with a fresh memory block; the clipboard must be open."""
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    ctypes.memmove(_kernel32.GlobalLock(handle), data, len(data))
    _kernel32.GlobalUnlock(handle)
    if not _user32.SetClipboardData(fmt, handle):
        _kernel32.GlobalFree(handle)  # only freed on failure, on success Windows owns it
        raise ctypes.WinError(ctypes.get_last_error())
