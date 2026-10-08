"""Smileys page: pick, view, import and delete smiley sets (JSON)."""

from __future__ import annotations

import os
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..controller import MorseController
from ..dictionary import DictionaryManager
from ..i18n import tr
from ..settings import Settings
from ..smileys import SmileyError, SmileySetInfo, delete_smiley_set, import_smiley_file, load_smiley_codes
from . import theme
from .theme import Button, RoundedBox, boxed, list_box


class SmileyPanel(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        controller: MorseController,
        dictionaries: DictionaryManager,
        apply_settings: Callable[[Settings], None],
        on_changed: Callable[[], None],
    ) -> None:
        super().__init__(master, bg=theme.P.bg)
        self._controller = controller
        self._dictionaries = dictionaries
        self._apply_settings = apply_settings
        self._on_changed = on_changed
        # entry 0 is "no smileys" (None), the sets follow
        self._entries: list[SmileySetInfo | None] = []
        self._index = 0
        self._first = True  # preselect the active set on first display

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = tk.Frame(self, bg=theme.P.bg)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 20))
        left.rowconfigure(0, weight=1)
        box, self._list = boxed(left, list_box)
        box.configure(width=270)
        box.grid(row=0, column=0, sticky="ns")
        self._list.bind("<<ListboxSelect>>", self._on_select)
        actions = tk.Frame(left, bg=theme.P.bg)
        actions.grid(row=1, column=0, sticky="w", pady=(12, 0))
        Button(actions, tr("alpha.import"), self._import).pack(side="left", padx=(0, 6))
        Button(actions, tr("alpha.delete"), self._delete, variant="plain").pack(side="left")

        right = tk.Frame(self, bg=theme.P.bg)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)

        self._title = tk.StringVar()
        tk.Label(
            right, textvariable=self._title, bg=theme.P.bg, fg=theme.P.text, font=theme.font(11, "bold"), anchor="w"
        ).grid(row=0, column=0, sticky="w")
        self._desc = tk.StringVar()
        tk.Label(
            right, textvariable=self._desc, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(9), anchor="w",
            justify="left", wraplength=480,
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        table = RoundedBox(right, fit=False)
        table.grid(row=2, column=0, sticky="nsew")
        self._tree = ttk.Treeview(table.body, columns=("code", "smiley"), show="headings", selectmode="none")
        self._tree.heading("code", text=tr("smiley.col.code"), anchor="w")
        self._tree.heading("smiley", text=tr("smiley.col.output"), anchor="w")
        self._tree.column("code", width=180, anchor="w")
        self._tree.column("smiley", width=120, anchor="w")
        self._tree.pack(fill="both", expand=True, padx=10, pady=10)

        self._status = tk.StringVar()
        tk.Label(
            right, textvariable=self._status, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(9), anchor="w",
            justify="left", wraplength=480,
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

        buttons = tk.Frame(right, bg=theme.P.bg)
        buttons.grid(row=4, column=0, sticky="w", pady=(12, 0))
        Button(buttons, tr("alpha.use"), self._use, variant="primary", min_width=100).pack(side="left", padx=(0, 6))
        Button(buttons, tr("alpha.folder"), self._open_folder, variant="plain").pack(side="left")

        self.refresh()

    def refresh(self) -> None:
        active = self._controller.settings.smileys
        self._entries = [None, *self._dictionaries.smiley_sets()]
        self._list.delete(0, "end")
        for i, entry in enumerate(self._entries):
            is_active = (entry is None and not active) or (entry is not None and entry.id == active)
            name = tr("smiley.none") if entry is None else entry.name
            self._list.insert("end", f"  {name}" + (f"  · {tr('alpha.active')}" if is_active else ""))
            if entry is not None and not entry.user:
                self._list.itemconfigure(i, fg=theme.P.muted)  # built-in sets dimmed
            if is_active and self._first:
                self._index = i
        self._first = False
        self._index = min(self._index, len(self._entries) - 1)
        self._list.selection_set(self._index)
        self._show()

    def _on_select(self, _event: object) -> None:
        selection = self._list.curselection()
        if selection:
            self._index = selection[0]
            self._show()

    def _show(self) -> None:
        self._tree.delete(*self._tree.get_children())
        entry = self._entries[self._index]
        if entry is None:
            self._title.set(tr("smiley.none"))
            self._desc.set(tr("smiley.none.desc"))
            self._status.set("")
            return
        self._title.set(f"{entry.name}   {entry.id}.json")
        self._desc.set(entry.description)
        try:
            codes = load_smiley_codes(entry.id, self._dictionaries.smiley_directory, self._dictionaries.user_smiley_directory)
        except SmileyError as exc:
            self._status.set(str(exc))
            return
        for code, smiley in codes.items():
            self._tree.insert("", "end", values=(code, smiley))
        self._status.set(tr("smiley.status", n=len(codes)))

    def _use(self) -> None:
        entry = self._entries[self._index]
        try:
            self._apply_settings(self._controller.settings.replace(smileys="" if entry is None else entry.id))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(tr("smiley.err.use"), str(exc), parent=self)
            return
        self.refresh()
        self._on_changed()

    def _import(self) -> None:
        folder = self._dictionaries.user_smiley_directory
        if folder is None:
            return
        paths = filedialog.askopenfilenames(
            parent=self, title=tr("smiley.dlg.import"), filetypes=[(tr("file.json"), "*.json"), (tr("file.all"), "*.*")]
        )
        errors: list[str] = []
        last: SmileySetInfo | None = None
        for path in paths:
            try:
                last = import_smiley_file(Path(path), self._dictionaries.smiley_directory, folder)
            except (SmileyError, OSError) as exc:
                errors.append(f"{Path(path).name}: {exc}")
        if last is not None:
            self.refresh()
            for i, entry in enumerate(self._entries):
                if entry is not None and entry.id == last.id:
                    self._list.selection_clear(0, "end")
                    self._list.selection_set(i)
                    self._index = i
                    self._show()
            self._on_changed()
        if errors:
            messagebox.showerror(tr("smiley.err.import"), "\n".join(errors), parent=self)

    def _delete(self) -> None:
        entry = self._entries[self._index]
        folder = self._dictionaries.user_smiley_directory
        title = tr("alpha.dlg.delete.title")
        if entry is None or not entry.user or folder is None:
            messagebox.showinfo(title, tr("smiley.dlg.delete.own"), parent=self)
            return
        if entry.id == self._controller.settings.smileys:
            messagebox.showinfo(title, tr("smiley.dlg.delete.active"), parent=self)
            return
        if not messagebox.askyesno(title, tr("smiley.dlg.delete.confirm", name=entry.name), parent=self):
            return
        try:
            delete_smiley_set(entry.id, folder)
        except (SmileyError, OSError) as exc:
            messagebox.showerror(tr("smiley.err.delete"), str(exc), parent=self)
            return
        self._index = 0
        self.refresh()
        self._on_changed()

    def _open_folder(self) -> None:
        folder = self._dictionaries.user_smiley_directory
        if folder is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]  # Windows only
        except (AttributeError, OSError):
            messagebox.showinfo("", str(folder), parent=self)
