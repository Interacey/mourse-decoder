"""Live test page: shows detected symbols and letters."""

from __future__ import annotations

import tkinter as tk

from ..controller import MorseController
from ..i18n import tr
from ..triggers import trigger_label
from . import theme
from .theme import Button, Card, boxed, label, text_box

MAX_LINES = 200


class LivePanel(tk.Frame):
    def __init__(self, master: tk.Misc, controller: MorseController) -> None:
        super().__init__(master, bg=theme.P.bg)
        self._controller = controller
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self._hint = tk.StringVar()
        tk.Label(
            self, textvariable=self._hint, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(10), justify="left",
            anchor="w", wraplength=640,
        ).grid(row=0, column=0, sticky="w")

        display = Card(self)
        display.grid(row=1, column=0, sticky="ew", pady=(16, 16))
        inner = tk.Frame(display.body, bg=theme.P.surface)
        inner.pack(fill="x", padx=22, pady=18)
        self._sequence = tk.StringVar(value=" ")
        tk.Label(
            inner, textvariable=self._sequence, bg=theme.P.surface, fg=theme.P.text, font=(theme.MONO, 26), anchor="w"
        ).pack(anchor="w")
        row = tk.Frame(inner, bg=theme.P.surface)
        row.pack(anchor="w", pady=(8, 0))
        label(row, tr("live.typed"), size=9, muted=True).pack(side="left")
        self._typed = tk.StringVar()
        tk.Label(row, textvariable=self._typed, bg=theme.P.surface, fg=theme.P.text, font=theme.font(11, "bold")).pack(
            side="left", padx=10
        )

        log_box, self._log = boxed(self, text_box, pad=4, height=8, state="disabled", wrap="none", undo=False)
        log_box.grid(row=2, column=0, sticky="nsew")

        Button(self, tr("live.clear"), self._clear).grid(row=3, column=0, sticky="e", pady=(12, 0))
        self.refresh()

    def refresh(self) -> None:
        s = self._controller.settings
        state = tr("live.state.active") if s.enabled else tr("live.state.paused")
        self._hint.set(
            tr("live.hint", key=trigger_label(s.trigger), alphabet=s.alphabet, state=state,
               dot=s.dot_threshold_ms, gap=s.letter_gap_ms)
        )

    # called on the UI thread via TrayApp.call_in_ui
    def add_symbol(self, symbol: str) -> None:
        self._sequence.set(self._sequence.get().strip() + symbol)

    def add_letter(self, sequence: str, text: str | None) -> None:
        self._sequence.set(" ")
        self._log.configure(state="normal")
        self._log.insert("end", f"{sequence:<12} {text if text is not None else '?'}\n")
        lines = int(self._log.index("end-1c").split(".")[0])
        if lines > MAX_LINES:
            self._log.delete("1.0", f"{lines - MAX_LINES + 1}.0")
        self._log.see("end")
        self._log.configure(state="disabled")
        self._typed.set(self._controller.typed_text[-60:])

    def _clear(self) -> None:
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")
        self._sequence.set(" ")
