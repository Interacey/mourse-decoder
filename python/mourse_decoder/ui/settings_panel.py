"""Settings page. Every change applies immediately (live preview) and is saved.

An invalid input, e.g. a half-typed number, isn't applied but reported below the form.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ..controller import MorseController
from ..dictionary import DictionaryManager
from ..i18n import available_languages, tr
from ..settings import THEME_MODES, Settings
from . import theme
from .form import parse_form
from .key_capture import CaptureBackend, KeyCaptureButton, ShortcutCaptureButton
from .theme import Card, ScrollFrame, Switch, field_box, label

_TIMING_FIELDS = (
    ("dot_threshold_ms", "timing.dot"),
    ("letter_gap_ms", "timing.letter"),
    ("word_gap_ms", "timing.word"),
)
TYPING_DELAY_MS = 450  # apply number fields once typing pauses


class SettingsPanel(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        controller: MorseController,
        dictionaries: DictionaryManager,
        apply_settings: Callable[[Settings], None],
        capture_backend: CaptureBackend | None = None,
    ) -> None:
        super().__init__(master, bg=theme.P.bg)
        self._controller = controller
        self._dictionaries = dictionaries
        self._apply = apply_settings
        self._alphabet_by_label: dict[str, str] = {}
        self._smiley_by_label: dict[str, str] = {}
        self._language_by_label: dict[str, str] = {}
        self._theme_by_label = {tr(f"theme.{m}"): m for m in THEME_MODES}
        self._loading = False  # form is being filled, don't report changes
        self._busy = False     # our own change is being applied, don't overwrite it
        self._job: str | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        scroll = ScrollFrame(self)
        scroll.grid(row=0, column=0, sticky="nsew")
        self._body = body = scroll.body
        body.columnconfigure(0, weight=1)

        label(body, tr("settings.live"), size=9, muted=True).grid(row=0, column=0, sticky="w", padx=6, pady=(0, 4))

        # input
        self._section(1, "settings.section.input")
        card = Card(body)
        card.grid(row=2, column=0, sticky="ew")

        self._enabled = tk.BooleanVar()
        self._enabled.trace_add("write", lambda *_: self._changed())
        Switch(card.add_row(tr("settings.input"), tr("settings.input.hint")), self._enabled).pack()

        key_row = card.add_row(tr("settings.key"), tr("settings.key.hint"))
        self._key_button = KeyCaptureButton(
            key_row, controller.settings.trigger, on_change=lambda _name: self._changed(),
            on_hint=self._show_key_hint, backend=capture_backend,
        )
        self._key_button.pack()
        self._key_hint = label(body, "", size=9, muted=True)

        self._block = tk.BooleanVar()
        self._block.trace_add("write", lambda *_: self._changed())
        Switch(card.add_row(tr("settings.block"), tr("settings.block.hint")), self._block).pack()

        shortcut_row = card.add_row(tr("settings.shortcut"), tr("settings.shortcut.hint"))
        self._shortcut_button = ShortcutCaptureButton(
            shortcut_row, controller.settings.block_shortcut, on_change=lambda _spec: self._changed(),
            on_hint=self._show_key_hint, backend=capture_backend,
        )
        self._shortcut_button.pack()

        self._alphabet_var = tk.StringVar()
        self._alphabet_box = self._combo(card.add_row(tr("settings.alphabet"), tr("settings.alphabet.hint")), self._alphabet_var)
        self._smiley_var = tk.StringVar()
        self._smiley_box = self._combo(card.add_row(tr("settings.smileys"), tr("settings.smileys.hint")), self._smiley_var)

        # display
        self._section(4, "settings.section.display")
        display = Card(body)
        display.grid(row=5, column=0, sticky="ew")
        self._language_var = tk.StringVar()
        self._language_box = self._combo(display.add_row(tr("settings.language"), tr("settings.language.hint")), self._language_var)
        self._notify = tk.BooleanVar()
        self._notify.trace_add("write", lambda *_: self._changed())
        Switch(display.add_row(tr("settings.notify"), tr("settings.notify.hint")), self._notify).pack()
        self._theme_var = tk.StringVar()
        self._combo(display.add_row(tr("settings.theme"), tr("settings.theme.hint")), self._theme_var, list(self._theme_by_label))

        # timing
        self._section(6, "settings.section.timing")
        timing = Card(body)
        timing.grid(row=7, column=0, sticky="ew")
        self._vars: dict[str, tk.StringVar] = {}
        for key, title in _TIMING_FIELDS:
            var = tk.StringVar()
            var.trace_add("write", lambda *_: self._schedule())
            holder = timing.add_row(tr(title), tr(f"{title}.hint"))
            label(holder, tr("unit.ms"), muted=True).pack(side="right", padx=(10, 0))
            box, _ = field_box(
                holder, ttk.Spinbox, textvariable=var, from_=0, to=15000, increment=50, width=7
            )
            box.configure(width=110, height=40)
            box.pack(side="right")
            self._vars[key] = var

        self._error = tk.Label(body, bg=theme.P.bg, fg=theme.P.danger, font=theme.font(9), anchor="w", justify="left")
        self._error.grid(row=8, column=0, sticky="w", padx=6, pady=(10, 0))

        self.refresh()

    def _section(self, row: int, key: str) -> None:
        label(self._body, tr(key), size=9, weight="bold", muted=True).grid(
            row=row, column=0, sticky="w", padx=6, pady=(18 if row > 1 else 4, 6)
        )

    def _combo(self, parent: tk.Misc, variable: tk.StringVar, values: list[str] | None = None) -> ttk.Combobox:
        box, combo = field_box(parent, ttk.Combobox, textvariable=variable, state="readonly", width=30, values=values or [])
        box.configure(width=300, height=40)
        box.pack()
        combo.bind("<<ComboboxSelected>>", lambda _e: self._changed())
        return combo

    def _show_key_hint(self, text: str) -> None:
        if text:
            self._key_hint.grid(row=3, column=0, sticky="w", padx=6, pady=(6, 0))
            self._key_hint.configure(text=text)
        else:
            self._key_hint.grid_remove()

    def refresh(self) -> None:
        """Fill the form from the current settings and files (not during our own change)."""
        if self._busy:
            return
        self._loading = True
        try:
            current = self._controller.settings
            self._enabled.set(current.enabled)
            for key, var in self._vars.items():
                var.set(str(getattr(current, key)))
            self._key_button.set_trigger(current.trigger)
            self._block.set(current.block_input)
            self._notify.set(current.notifications)
            self._shortcut_button.set_shortcut(current.block_shortcut)

            infos = self._dictionaries.available()
            self._alphabet_by_label = {a.name: a.id for a in infos}
            self._alphabet_box.configure(values=list(self._alphabet_by_label))
            self._alphabet_var.set(next((a.name for a in infos if a.id == current.alphabet), current.alphabet))

            sets = self._dictionaries.smiley_sets()
            self._smiley_by_label = {tr("smileys.off"): "", **{s.name: s.id for s in sets}}
            self._smiley_box.configure(values=list(self._smiley_by_label))
            self._smiley_var.set(next((s.name for s in sets if s.id == current.smileys), tr("smileys.off")))

            languages = available_languages()
            self._language_by_label = {name: code for code, name in languages}
            self._language_box.configure(values=list(self._language_by_label))
            self._language_var.set(next((n for c, n in languages if c == current.language), current.language))
            self._theme_var.set(next((n for n, m in self._theme_by_label.items() if m == current.theme), tr("theme.system")))
            self._error.configure(text="")
        finally:
            self._loading = False

    def _schedule(self) -> None:
        if self._loading:
            return
        if self._job is not None:
            self.after_cancel(self._job)
        self._job = self.after(TYPING_DELAY_MS, self._changed)

    def _changed(self) -> None:
        """Read the form and apply it right away (live preview)."""
        if self._loading or self._busy:
            return
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        current = self._controller.settings
        values = {key: var.get() for key, var in self._vars.items()}
        values["trigger"] = self._key_button.trigger
        values["alphabet"] = self._alphabet_by_label.get(self._alphabet_var.get(), current.alphabet)
        settings, errors = parse_form(values, current)
        if errors or settings is None:
            self._error.configure(text="\n".join(errors))
            return
        settings = settings.replace(
            enabled=self._enabled.get(),
            block_input=self._block.get(),
            notifications=self._notify.get(),
            block_shortcut=self._shortcut_button.shortcut,
            smileys=self._smiley_by_label.get(self._smiley_var.get(), current.smileys),
            language=self._language_by_label.get(self._language_var.get(), current.language),
            theme=self._theme_by_label.get(self._theme_var.get(), current.theme),
        )
        if settings == current:
            self._error.configure(text="")
            return
        self._busy = True
        try:
            self._apply(settings)
            self._error.configure(text="")
        except Exception as exc:  # noqa: BLE001 - e.g. a broken alphabet file
            self._error.configure(text=f"{tr('err.apply')}: {exc}")
            self._busy = False
            self.refresh()  # reset the form to what is actually active
        finally:
            self._busy = False
