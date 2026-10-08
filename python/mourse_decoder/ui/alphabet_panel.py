"""Alphabets page: list, create, import, edit and delete JSON files.

Built-in alphabets are read-only (view or "Create copy"). User alphabets are
fully validated on save; if that fails the old file stays untouched.
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox

from ..alphabet_files import (
    delete_alphabet,
    export_alphabet_file,
    import_alphabet_file,
    new_alphabet_text,
    parse_alphabet_text,
    read_alphabet_text,
    save_alphabet_text,
    slugify,
)
from ..controller import MorseController
from ..dictionary import AlphabetError, AlphabetInfo, DictionaryManager
from ..i18n import tr
from ..settings import Settings
from . import theme
from .theme import Button, boxed, list_box, text_box


class AlphabetPanel(tk.Frame):
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
        self._infos: list[AlphabetInfo] = []
        # selected alphabet, None for a new unsaved one
        self._selected: AlphabetInfo | None = None
        self._selected_index: int | None = None

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # left: list and actions
        left = tk.Frame(self, bg=theme.P.bg)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 20))
        left.rowconfigure(0, weight=1)
        box, self._list = boxed(left, list_box)
        box.configure(width=270)
        box.grid(row=0, column=0, sticky="ns")
        self._list.bind("<<ListboxSelect>>", self._on_select)

        actions = tk.Frame(left, bg=theme.P.bg)
        actions.grid(row=1, column=0, sticky="w", pady=(12, 0))
        Button(actions, tr("alpha.new"), self._new).pack(side="left", padx=(0, 6))
        Button(actions, tr("alpha.import"), self._import).pack(side="left", padx=(0, 6))
        Button(actions, tr("alpha.delete"), self._delete, variant="plain").pack(side="left")

        # right: editor
        right = tk.Frame(self, bg=theme.P.bg)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self._title = tk.StringVar(value=tr("alpha.empty"))
        tk.Label(
            right, textvariable=self._title, bg=theme.P.bg, fg=theme.P.text, font=theme.font(11, "bold"), anchor="w"
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        editor, self._text = boxed(right, text_box, pad=4, wrap="none", width=40, height=10)
        editor.grid(row=1, column=0, sticky="nsew")

        self._status = tk.StringVar()
        tk.Label(
            right, textvariable=self._status, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(9), anchor="w",
            justify="left", wraplength=520,
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))

        buttons = tk.Frame(right, bg=theme.P.bg)
        buttons.grid(row=3, column=0, sticky="w", pady=(12, 0))
        self._save_btn = Button(buttons, tr("alpha.save"), self._save, variant="primary", min_width=100)
        self._copy_btn = Button(buttons, tr("alpha.copy"), self._copy)
        self._use_btn = Button(buttons, tr("alpha.use"), self._activate)
        self._save_btn.grid(row=0, column=0, padx=(0, 6))
        Button(buttons, tr("alpha.check"), self._check).grid(row=0, column=1, padx=(0, 6))
        self._copy_btn.grid(row=0, column=2)
        self._use_btn.grid(row=1, column=0, padx=(0, 6), pady=(8, 0))
        Button(buttons, tr("alpha.export"), self._export).grid(row=1, column=1, padx=(0, 6), pady=(8, 0))
        Button(buttons, tr("alpha.folder"), self._open_folder, variant="plain").grid(row=1, column=2, sticky="w", pady=(8, 0))

        self.refresh()
        self._set_editable(False)

    def refresh(self, select_id: str | None = None) -> None:
        """Reread the list; ``select_id`` selects an alphabet afterwards."""
        active = self._controller.settings.alphabet
        self._infos = self._dictionaries.available()
        self._list.delete(0, "end")
        for i, info in enumerate(self._infos):
            self._list.insert("end", f"  {info.name}" + (f"  · {tr('alpha.active')}" if info.id == active else ""))
            if not info.user:
                self._list.itemconfigure(i, fg=theme.P.muted)  # built-in alphabets dimmed
        target = select_id or (self._selected.id if self._selected else None)
        index = next((i for i, info in enumerate(self._infos) if info.id == target), None)
        if index is None:
            self._selected_index = None
            return
        self._list.selection_set(index)
        self._selected_index = index
        # a plain list refresh must not overwrite unsaved edits
        if select_id is not None or not self._text.edit_modified():
            self._show(self._infos[index])
        else:
            self._selected = self._infos[index]

    def _on_select(self, _event: object) -> None:
        selection = self._list.curselection()
        if not selection:
            return
        index = selection[0]
        if index == self._selected_index:
            return
        if not self._confirm_discard():
            self._list.selection_clear(0, "end")
            if self._selected_index is not None:
                self._list.selection_set(self._selected_index)
            return
        self._selected_index = index
        self._show(self._infos[index])

    def _confirm_discard(self) -> bool:
        if not self._text.edit_modified():
            return True
        return messagebox.askyesno(tr("alpha.dlg.discard.title"), tr("alpha.dlg.discard"), parent=self)

    def _set_editable(self, editable: bool) -> None:
        self._text.configure(state="normal" if editable else "disabled")
        self._save_btn.set_enabled(editable)

    def _set_text(self, text: str) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", text)
        self._text.edit_reset()
        self._text.edit_modified(False)

    def _show(self, info: AlphabetInfo) -> None:
        self._selected = info
        try:
            self._set_text(read_alphabet_text(info))
        except OSError as exc:
            self._set_text("")
            self._status.set(tr("alpha.status.unreadable", msg=exc))
        else:
            self._status.set("" if info.user else tr("alpha.status.builtin"))
        self._set_editable(info.user)
        self._title.set(f"{info.name}   {info.id}.json" + ("" if info.user else f"   {tr('alpha.viewonly')}"))
        self._copy_btn.set_enabled(True)
        self._use_btn.set_enabled(True)

    def _start_draft(self, text: str, title: str, status: str, modified: bool = False) -> None:
        self._list.selection_clear(0, "end")
        self._selected = None
        self._selected_index = None
        self._set_text(text)
        self._text.edit_modified(modified)
        self._set_editable(True)
        self._copy_btn.set_enabled(False)
        self._use_btn.set_enabled(False)
        self._title.set(title)
        self._status.set(status)

    def _new(self) -> None:
        if self._confirm_discard():
            self._start_draft(new_alphabet_text(), tr("alpha.title.new"), tr("alpha.status.new"))

    def _copy(self) -> None:
        if self._selected is None:
            return
        try:
            data = parse_alphabet_text(self._text.get("1.0", "end"))
        except AlphabetError as exc:
            messagebox.showerror(tr("alpha.err.copy"), str(exc), parent=self)
            return
        taken = {i.id for i in self._infos}
        base = slugify(f"{self._selected.id}-eigen")
        new_id, n = base, 2
        while new_id in taken:
            new_id, n = f"{base}-{n}", n + 1
        data = {**data, "id": new_id, "name": f"{data.get('name', self._selected.id)} (Copy)"}
        data.pop("abstract", None)
        self._start_draft(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            tr("alpha.title.copy"),
            tr("alpha.status.copy"),
            modified=True,
        )

    def _check(self) -> None:
        try:
            data = parse_alphabet_text(self._text.get("1.0", "end"))
        except AlphabetError as exc:
            self._status.set(tr("alpha.status.json_err", msg=exc))
            return
        codes = data.get("codes", {})
        self._status.set(tr("alpha.status.json_ok", n=len(codes) if isinstance(codes, dict) else 0))

    def _save(self) -> None:
        text = self._text.get("1.0", "end")
        replacing = self._selected.id if self._selected else None
        active = self._controller.settings
        try:
            info = save_alphabet_text(self._dictionaries, text, replacing=replacing)
            if active.alphabet == replacing and replacing != info.id:
                self._apply_settings(active.replace(alphabet=info.id))  # renamed: move the active alphabet along
            else:
                self._controller.reload_alphabet()
        except Exception as exc:  # noqa: BLE001 - AlphabetError, ValueError, OSError
            self._status.set(tr("alpha.status.not_saved", msg=exc))
            messagebox.showerror(tr("alpha.err.not_saved"), str(exc), parent=self)
            return
        self._text.edit_modified(False)
        self._selected = None  # refresh reselects by id
        self.refresh(select_id=info.id)
        self._on_changed()
        self._status.set(tr("alpha.status.saved", path=info.path))  # after on_changed, which redraws the list

    def _activate(self) -> None:
        if self._selected is None:
            return
        try:
            self._apply_settings(self._controller.settings.replace(alphabet=self._selected.id))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(tr("alpha.err.use"), str(exc), parent=self)
            return
        self.refresh()
        self._on_changed()

    def _import(self) -> None:
        if not self._confirm_discard():
            return
        paths = filedialog.askopenfilenames(
            parent=self, title=tr("alpha.dlg.import"), filetypes=[(tr("file.json"), "*.json"), (tr("file.all"), "*.*")]
        )
        imported: list[str] = []
        errors: list[str] = []
        last_id: str | None = None
        for path in paths:
            try:
                info = import_alphabet_file(self._dictionaries, Path(path))
            except (AlphabetError, OSError) as exc:
                errors.append(f"{Path(path).name}: {exc}")
            else:
                imported.append(info.name)
                last_id = info.id
        if imported:
            self._selected = None
            self._text.edit_modified(False)
            self.refresh(select_id=last_id)
            self._on_changed()
            self._status.set(tr("alpha.status.imported", names=", ".join(imported)))
        if errors:
            messagebox.showerror(tr("alpha.err.import"), "\n".join(errors), parent=self)

    def _export(self) -> None:
        if self._selected is None:
            return
        target = filedialog.asksaveasfilename(
            parent=self,
            title=tr("alpha.dlg.export"),
            initialfile=f"{self._selected.id}.json",
            defaultextension=".json",
            filetypes=[(tr("file.json"), "*.json")],
        )
        if target:
            try:
                export_alphabet_file(self._selected, Path(target))
            except OSError as exc:
                messagebox.showerror(tr("alpha.err.export"), str(exc), parent=self)

    def _delete(self) -> None:
        info = self._selected
        title = tr("alpha.dlg.delete.title")
        if info is None or not info.user:
            messagebox.showinfo(title, tr("alpha.dlg.delete.own"), parent=self)
            return
        if info.id == self._controller.settings.alphabet:
            messagebox.showinfo(title, tr("alpha.dlg.delete.active"), parent=self)
            return
        if not messagebox.askyesno(title, tr("alpha.dlg.delete.confirm", name=info.name, id=info.id), parent=self):
            return
        try:
            delete_alphabet(self._dictionaries, info.id)
        except (AlphabetError, OSError) as exc:
            messagebox.showerror(tr("alpha.err.delete"), str(exc), parent=self)
            return
        self._selected = None
        self._selected_index = None
        self._set_text("")
        self._set_editable(False)
        self._title.set(tr("alpha.title.deleted"))
        self.refresh()
        self._on_changed()

    def _open_folder(self) -> None:
        folder = self._dictionaries.user_directory
        if folder is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]  # Windows only
        except (AttributeError, OSError):
            messagebox.showinfo("", str(folder), parent=self)
