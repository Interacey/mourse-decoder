"""Translator page: shows the typed text as Morse code live, using the active alphabet."""

from __future__ import annotations

import tkinter as tk

from ..controller import MorseController
from ..dictionary import AlphabetError, DictionaryManager
from ..i18n import tr
from ..translator import encode_text
from . import theme
from .theme import Button, boxed, label, text_box


class TranslatorPanel(tk.Frame):
    def __init__(self, master: tk.Misc, controller: MorseController, dictionaries: DictionaryManager) -> None:
        super().__init__(master, bg=theme.P.bg)
        self._controller = controller
        self._dictionaries = dictionaries
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.rowconfigure(5, weight=1)

        self._hint = tk.StringVar()
        tk.Label(self, textvariable=self._hint, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(10), anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        label(self, tr("translator.input"), size=9, weight="bold", muted=True).grid(row=1, column=0, sticky="w", pady=(14, 6))
        box, self._input = boxed(self, text_box, pad=4, height=5, wrap="word", font=theme.font(12))
        box.grid(row=2, column=0, sticky="nsew")
        self._input.bind("<<Modified>>", self._on_modified)

        label(self, tr("translator.output"), size=9, weight="bold", muted=True).grid(row=3, column=0, sticky="w", pady=(14, 6))
        self._status = tk.StringVar()
        tk.Label(self, textvariable=self._status, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(9), anchor="w").grid(
            row=4, column=0, sticky="w"
        )
        out_box, self._output = boxed(self, text_box, pad=4, height=5, wrap="word", undo=False, state="disabled")
        out_box.grid(row=5, column=0, sticky="nsew", pady=(6, 0))
        self._output.configure(font=(theme.MONO, 13))

        buttons = tk.Frame(self, bg=theme.P.bg)
        buttons.grid(row=6, column=0, sticky="e", pady=(12, 0))
        Button(buttons, tr("translator.clear"), self._clear, variant="plain").pack(side="left", padx=(0, 6))
        Button(buttons, tr("translator.copy"), self._copy, variant="primary", min_width=110).pack(side="left")
        self.refresh()

    def refresh(self) -> None:
        """The alphabet or smileys may have changed: update the hint and the output."""
        self._hint.set(tr("translator.hint", alphabet=self._alphabet_name()))
        self._translate()

    def _alphabet_name(self) -> str:
        alphabet_id = self._controller.settings.alphabet
        return next((a.name for a in self._dictionaries.available() if a.id == alphabet_id), alphabet_id)

    def _alphabet(self):
        alphabet_id = self._controller.settings.alphabet
        if alphabet_id == DictionaryManager.TELECODE_ID:  # telecodes aren't a JSON alphabet, use digits and punctuation
            return self._dictionaries.load("common")
        return self._dictionaries.select(alphabet_id)

    def _on_modified(self, _event: tk.Event) -> None:
        if self._input.edit_modified():
            self._input.edit_modified(False)
            self._translate()

    def _translate(self) -> None:
        try:
            result = encode_text(self._alphabet(), self._input.get("1.0", "end-1c"))
        except AlphabetError as exc:
            self._set_output(str(exc), "")
            return
        status = tr("translator.unknown", chars=" ".join(result.unknown)) if result.unknown else ""
        self._set_output(result.morse, status)

    def _set_output(self, text: str, status: str) -> None:
        self._output.configure(state="normal")
        self._output.delete("1.0", "end")
        self._output.insert("1.0", text)
        self._output.configure(state="disabled")
        self._status.set(status)

    def _copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._output.get("1.0", "end-1c"))
        self._status.set(tr("translator.copied"))

    def _clear(self) -> None:
        self._input.delete("1.0", "end")
        self._translate()
