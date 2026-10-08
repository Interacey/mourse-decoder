"""Splash screen: mouse icon, name and a small Morse loading bar."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from PIL import ImageTk

from ..i18n import tr
from . import theme
from .icon import create_icon

# the bar keys "MOUSE" (-- --- ..- ... .): dashes long, dots short
_PATTERN = "-- --- ..- ... ."


class Splash(tk.Toplevel):
    WIDTH, HEIGHT = 440, 320

    def __init__(self, master: tk.Misc, on_done: Callable[[], None] | None = None, duration_ms: int = 1800) -> None:
        super().__init__(master, bg=theme.P.surface)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        x = (self.winfo_screenwidth() - self.WIDTH) // 2
        y = (self.winfo_screenheight() - self.HEIGHT) // 3
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")
        self._on_done = on_done

        body = tk.Frame(self, bg=theme.P.surface)
        body.pack(fill="both", expand=True)
        self._logo = ImageTk.PhotoImage(create_icon(True, 112))
        tk.Label(body, image=self._logo, bg=theme.P.surface).pack(pady=(34, 10))
        tk.Label(body, text=tr("app.name"), bg=theme.P.surface, fg=theme.P.text, font=theme.font(22, "bold")).pack()
        tk.Label(body, text=tr("splash.loading"), bg=theme.P.surface, fg=theme.P.muted, font=theme.font(10)).pack(pady=(4, 18))

        self._bar = tk.Canvas(body, width=220, height=10, bg=theme.P.surface, highlightthickness=0, bd=0)
        self._bar.pack()
        self._marks = self._layout(_PATTERN)
        self._shown = 0
        self._step = max(duration_ms // (len(self._marks) + 1), 40)
        self.after(self._step, self._tick)
        self.after(duration_ms, self._finish)
        self.update_idletasks()
        theme.clip_round(self, self.WIDTH, self.HEIGHT, 30)

    @staticmethod
    def _layout(pattern: str) -> list[tuple[int, int]]:
        """Pixel ranges (x1, x2) of the marks; a space is a gap."""
        marks, x = [], 0
        for ch in pattern:
            if ch == ".":
                marks.append((x, x + 8))
                x += 8 + 6
            elif ch == "-":
                marks.append((x, x + 24))
                x += 24 + 6
            else:
                x += 8
        offset = (220 - (x - 6)) // 2
        return [(a + offset, b + offset) for a, b in marks]

    def _tick(self) -> None:
        if self._shown >= len(self._marks) or not self.winfo_exists():
            return
        a, b = self._marks[self._shown]
        theme._round_rect(self._bar, a, 1, b, 9, 4, fill=theme.P.accent_text, outline="")
        self._shown += 1
        self.after(self._step, self._tick)

    def _finish(self) -> None:
        if not self.winfo_exists():
            return
        self.destroy()
        if self._on_done is not None:
            self._on_done()
