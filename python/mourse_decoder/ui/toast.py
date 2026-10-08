"""A small, quiet message that fades in and out without taking focus or playing a sound."""

from __future__ import annotations

import sys
import tkinter as tk

from . import theme

HOLD_MS = 1300
FADE_STEP_MS = 25
MAX_ALPHA = 0.94


class Toast:
    def __init__(self, root: tk.Misc) -> None:
        self._root = root
        self._window: tk.Toplevel | None = None
        self._job: str | None = None

    def show(self, text: str, on: bool = True) -> None:
        """Show ``text`` in a pill near the bottom of the screen; a dot shows on/off."""
        self.close()
        p = theme.P
        window = tk.Toplevel(self._root, bg=p.surface)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.attributes("-alpha", 0.0)
        row = tk.Frame(window, bg=p.surface)
        row.pack(padx=22, pady=11)
        dot = tk.Canvas(row, width=10, height=10, bg=p.surface, highlightthickness=0, bd=0)
        dot.create_oval(1, 1, 9, 9, fill=p.accent_text if on else p.muted, outline="")
        dot.pack(side="left", padx=(0, 10))
        tk.Label(row, text=text, bg=p.surface, fg=p.text, font=theme.font(10, "bold")).pack(side="left")
        window.update_idletasks()
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        x = (window.winfo_screenwidth() - width) // 2
        y = window.winfo_screenheight() - height - 120
        window.geometry(f"{width}x{height}+{x}+{y}")
        theme.clip_round(window, width, height, height // 2)
        self._no_focus(window)
        self._window = window
        self._fade(0.0, +MAX_ALPHA / 6)

    @staticmethod
    def _no_focus(window: tk.Toplevel) -> None:
        """Never activate, never show in the taskbar, let clicks pass through (Windows only)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
            style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080 | 0x00000020)
        except (AttributeError, OSError):
            pass

    def _fade(self, alpha: float, step: float) -> None:
        window = self._window
        if window is None or not window.winfo_exists():
            return
        alpha = min(max(alpha + step, 0.0), MAX_ALPHA)
        window.attributes("-alpha", alpha)
        if step > 0 and alpha < MAX_ALPHA:
            self._job = window.after(FADE_STEP_MS, self._fade, alpha, step)
        elif step > 0:
            self._job = window.after(HOLD_MS, self._fade, alpha, -MAX_ALPHA / 8)
        elif alpha > 0:
            self._job = window.after(FADE_STEP_MS, self._fade, alpha, step)
        else:
            self.close()

    def close(self) -> None:
        window, self._window = self._window, None
        if window is not None and window.winfo_exists():
            window.destroy()
