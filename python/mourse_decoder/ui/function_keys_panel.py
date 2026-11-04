"""Function keys page: the 100 slots as a list, and an editor for the selected one.

Unlike the settings page, changes are not applied while typing: a slot is saved with the button,
after the macro and the trigger have been checked. Saving installs the slots in the hook first and
writes ``function_keys.json`` second; if writing fails the previous slots are installed again, so the
running program and the file never disagree.
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from ..controller import MorseController
from ..function_keys import FunctionKeys, FunctionKeysStore, Slot, default_trigger
from ..i18n import tr
from . import function_keys_view as view
from . import theme
from .key_capture import CaptureBackend, ShortcutCaptureButton
from .theme import Button, Card, RoundedBox, ScrollFrame, Switch, boxed, field_box, label, text_box
from .toast import Toast

log = logging.getLogger(__name__)

VALIDATE_DELAY_MS = 250  # check the editor once typing pauses
TEST_DELAY_MS = 3000     # the test types into the window in front: time to click into the target
EXAMPLE_MACRO = "{Loop 3 i}[i] {EndLoop}"  # types 0 1 2
REPEAT_EXAMPLES = ("{Backspace 5}", "{Space 18}")  # key name plus count
ESCAPED_BRACE = "\\{"  # how a literal "{" is written in a macro
_COLUMNS = ("slot", "kind", "trigger", "text", "status")
_WIDTHS = {"slot": 56, "kind": 72, "trigger": 200, "text": 120, "status": 84}


class FunctionKeysPanel(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        controller: MorseController,
        store: FunctionKeysStore,
        capture_backend: CaptureBackend | None = None,
        toast: Toast | None = None,
    ) -> None:
        super().__init__(master, bg=theme.P.bg)
        self._controller = controller
        self._store = store
        self._toast = toast or Toast(self.winfo_toplevel())
        self._capture_backend = capture_backend
        self._keys, self._load_warnings = store.load(controller.check_shortcut)
        self._number = 0          # slot in the editor
        self._loading = False     # editor is being filled, don't report changes
        self._selecting = False   # we change the selection ourselves
        self._jobs: dict[str, str] = {}
        self._wrapped: list[tk.Label] = []
        self._width = 0

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1, minsize=170)  # list: four rows and the header
        self.rowconfigure(4, weight=2, minsize=120)  # editor

        self._hint = tk.Label(
            self, text=tr("functionkeys.hint"), bg=theme.P.bg, fg=theme.P.muted, font=theme.font(10),
            anchor="w", justify="left",
        )
        self._hint.grid(row=0, column=0, sticky="ew")
        self._wrapped.append(self._hint)
        self._build_notice()
        self._build_filter()
        self._build_list()
        self._build_editor()
        self.bind("<Configure>", self._on_resize)
        self.bind("<Destroy>", self._on_destroy)

        self._fill_list()
        self._update_notice()
        first = next((s.slot for s in self._keys.slots if view.is_used(s)), 13)
        self._select(first)
        self._load_editor(first)

    # layout

    def _build_notice(self) -> None:
        self._notice = RoundedBox(self)
        inner = tk.Frame(self._notice.body, bg=theme.P.surface)
        inner.pack(fill="x", padx=24, pady=12)
        label(inner, tr("functionkeys.notice")).pack(anchor="w")
        self._notice_text = label(inner, "", size=9, muted=True, justify="left")
        self._notice_text.pack(anchor="w", pady=(2, 0))
        self._wrapped.append(self._notice_text)

    def _build_filter(self) -> None:
        bar = tk.Frame(self, bg=theme.P.bg)
        bar.grid(row=2, column=0, sticky="ew", pady=(16, 8))
        label(bar, tr("functionkeys.search"), size=9, weight="bold", muted=True).pack(side="left", padx=(8, 8))
        self._query = tk.StringVar()
        self._query.trace_add("write", lambda *_: self._fill_list())
        box, _ = field_box(bar, ttk.Entry, textvariable=self._query, width=24)
        box.configure(width=240, height=40)
        box.pack(side="left")
        self._only_used = tk.BooleanVar()
        self._only_used.trace_add("write", lambda *_: self._fill_list())
        Switch(bar, self._only_used).pack(side="right")
        label(bar, tr("functionkeys.only_used")).pack(side="right", padx=(16, 8))
        self._count = label(bar, "", size=9, muted=True)
        self._count.pack(side="right", padx=(8, 0))

    def _build_list(self) -> None:
        table = RoundedBox(self, fit=False)
        table.grid(row=3, column=0, sticky="nsew")
        self._tree = ttk.Treeview(table.body, columns=_COLUMNS, show="headings", selectmode="browse", height=4)
        for name in _COLUMNS:
            self._tree.heading(name, text=tr(f"functionkeys.col.{name}"), anchor="w")
            self._tree.column(name, width=_WIDTHS[name], anchor="w", stretch=name == "text")
        self._tree.tag_configure("dim", foreground=theme.P.muted)
        bar = ttk.Scrollbar(table.body, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self._tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

    def _build_editor(self) -> None:
        scroll = ScrollFrame(self)
        scroll.grid(row=4, column=0, sticky="nsew", pady=(16, 0))
        body = scroll.body
        body.columnconfigure(0, weight=1)

        self._title = tk.StringVar()
        tk.Label(
            body, textvariable=self._title, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(9, "bold"), anchor="w"
        ).grid(row=0, column=0, sticky="w", padx=8, pady=(4, 8))

        macro_box, self._macro = boxed(body, text_box, pad=4, height=5, wrap="word")
        macro_box.configure(height=110)
        macro_box.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self._macro.bind("<<Modified>>", self._on_macro_modified)
        self._error = tk.Label(body, bg=theme.P.bg, fg=theme.P.danger, font=theme.font(9), anchor="w", justify="left")
        self._error.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 8))
        self._error.grid_remove()
        self._wrapped.append(self._error)
        help_text = " ".join((
            tr("functionkeys.macro.help", example=EXAMPLE_MACRO),
            tr("functionkeys.macro.repeat", first=REPEAT_EXAMPLES[0], second=REPEAT_EXAMPLES[1]),
            tr("functionkeys.macro.escape", brace=ESCAPED_BRACE),
        ))
        macro_help = label(body, help_text, size=9, muted=True, justify="left")
        macro_help.grid(row=5, column=0, sticky="w", padx=8, pady=(8, 0))
        self._wrapped.append(macro_help)

        card = Card(body)
        card.grid(row=2, column=0, sticky="ew")
        self._label_var = tk.StringVar()
        self._label_var.trace_add("write", lambda *_: self._changed())
        box, _ = field_box(
            card.add_row(tr("functionkeys.label"), tr("functionkeys.label.hint")),
            ttk.Entry, textvariable=self._label_var, width=30,
        )
        box.configure(width=300, height=40)
        box.pack()

        trigger_row = card.add_row(tr("functionkeys.trigger"), tr("functionkeys.trigger.hint"))
        Button(trigger_row, tr("functionkeys.trigger.default"), self._default_trigger, variant="plain").pack(
            side="right", padx=(8, 0)
        )
        self._trigger = ShortcutCaptureButton(
            trigger_row, "", on_change=lambda _spec: self._changed(), on_hint=self._show_hint,
            backend=self._capture_backend,
        )
        self._trigger.pack(side="right")

        self._enabled = tk.BooleanVar()
        self._enabled.trace_add("write", lambda *_: self._changed())
        Switch(card.add_row(tr("functionkeys.enabled"), tr("functionkeys.enabled.hint")), self._enabled).pack()

        self._swallow = tk.BooleanVar(value=True)
        self._swallow.trace_add("write", lambda *_: self._changed())
        swallow_row = card.add_row(tr("functionkeys.swallow"), tr("functionkeys.swallow.hint"))
        if sys.platform == "win32":
            Switch(swallow_row, self._swallow).pack()
        else:  # the hook can only watch there, the key always reaches the program
            label(swallow_row, tr("functionkeys.swallow.linux"), size=9, muted=True).pack()

        self._key_hint = label(body, "", size=9, muted=True, justify="left")
        self._key_hint.grid(row=3, column=0, sticky="w", padx=8, pady=(8, 0))
        self._key_hint.grid_remove()
        self._wrapped.append(self._key_hint)

        buttons = tk.Frame(self, bg=theme.P.bg)
        buttons.grid(row=5, column=0, sticky="e", pady=(12, 0))
        self._reset_button = Button(buttons, tr("functionkeys.reset"), self._reset, variant="plain")
        self._reset_button.pack(side="left", padx=(0, 8))
        self._test_button = Button(buttons, tr("functionkeys.test"), self._test)
        self._test_button.pack(side="left", padx=(0, 8))
        self._save_button = Button(buttons, tr("functionkeys.save"), self._save, variant="primary", min_width=110)
        self._save_button.pack(side="left")

    def _on_resize(self, event: tk.Event) -> None:
        if event.widget is not self or event.width == self._width:
            return
        self._width = event.width
        for text in self._wrapped:
            text.configure(wraplength=max(event.width - 72, 200))

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            for job in self._jobs.values():
                self.after_cancel(job)
            self._jobs.clear()

    def _later(self, name: str, delay: int, command) -> None:
        """Run ``command`` after ``delay`` ms; a second call with the same name replaces the first."""
        if (old := self._jobs.pop(name, None)) is not None:
            self.after_cancel(old)
        self._jobs[name] = self.after(delay, lambda: (self._jobs.pop(name, None), command()))

    # list

    def _fill_list(self) -> None:
        shown = view.filter_slots(self._keys, self._query.get(), self._only_used.get())
        self._tree.delete(*self._tree.get_children())
        for slot in shown:
            name, kind, trigger, text, state = view.row(slot)
            self._tree.insert(
                "", "end", iid=str(slot.slot), tags=("dim",) if state in ("empty", "off") else (),
                values=(name, tr(f"functionkeys.type.{kind}"), trigger or "-", text, tr(f"functionkeys.status.{state}")),
            )
        self._count.configure(text=tr("functionkeys.count", shown=len(shown), total=len(self._keys.slots)))
        if self._number:
            self._select(self._number)

    def _select(self, number: int) -> None:
        iid = str(number)
        if not self._tree.exists(iid):
            return
        self._selecting = True
        try:
            self._tree.selection_set(iid)
            self._tree.see(iid)
        finally:
            self._selecting = False

    def _on_select(self, _event: object) -> None:
        selection = self._tree.selection()
        if self._selecting or not selection or int(selection[0]) == self._number:
            return
        if self._dirty() and not messagebox.askyesno(
            tr("functionkeys.discard.title"), tr("functionkeys.discard", name=self._keys.slot(self._number).name),
            parent=self,
        ):
            self._select(self._number)
            return
        self._load_editor(int(selection[0]))

    # editor

    def _load_editor(self, number: int) -> None:
        slot = self._keys.slot(number)
        self._loading = True
        try:
            self._number = number
            self._title.set(tr("functionkeys.editor", name=slot.name, kind=tr(f"functionkeys.type.{view.row(slot)[1]}")))
            self._label_var.set(slot.label)
            self._trigger.set_shortcut(slot.trigger)
            self._enabled.set(slot.enabled)
            self._swallow.set(slot.swallow)
            self._macro.delete("1.0", "end")
            self._macro.insert("1.0", slot.macro)
            self._macro.edit_reset()
            self._macro.edit_modified(False)
            self._key_hint.grid_remove()
        finally:
            self._loading = False
        self._validate()

    def _editor_slot(self) -> Slot:
        return self._keys.slot(self._number).replace(
            label=self._label_var.get(), trigger=self._trigger.shortcut, enabled=self._enabled.get(),
            swallow=self._swallow.get(), macro=self._macro.get("1.0", "end-1c"),
        )

    def _dirty(self) -> bool:
        return bool(self._number) and not self._editor_slot().same_content(self._keys.slot(self._number))

    def _on_macro_modified(self, _event: object) -> None:
        if self._macro.edit_modified():
            self._macro.edit_modified(False)
            self._changed()

    def _changed(self) -> None:
        if self._loading or not self._number:
            return
        self._update_buttons()
        self._later("validate", VALIDATE_DELAY_MS, self._validate)

    def _show_hint(self, text: str) -> None:
        if text:
            self._key_hint.configure(text=text)
            self._key_hint.grid()
        else:
            self._key_hint.grid_remove()

    def _default_trigger(self) -> None:
        self._trigger.set_shortcut(default_trigger(self._number))
        self._changed()

    def _problems(self, keys: FunctionKeys) -> list[str]:
        return keys.validate(self._controller.settings, self._controller.check_shortcut)

    def _validate(self) -> None:
        """Show what is wrong with the editor's slot: its macro, its trigger, a clash with another slot."""
        slot = self._editor_slot()
        problems = view.errors_for_slot(self._problems(self._keys.replace_slot(slot)), slot.slot)
        self._show_error("\n".join(problems))
        self._update_buttons(valid=not problems)

    def _show_error(self, text: str) -> None:
        self._error.configure(text=text)
        if text:
            self._error.grid()
        else:
            self._error.grid_remove()

    def _update_buttons(self, valid: bool | None = None) -> None:
        if valid is None:
            valid = not self._error.cget("text")
        dirty = self._dirty()
        self._save_button.set_enabled(dirty and valid and not self._store.read_only)
        self._reset_button.set_enabled(not self._store.read_only)
        self._test_button.set_enabled(not dirty)

    # actions

    def _commit(self, new: FunctionKeys) -> bool:
        """Install ``new`` and write it. False, with the reason in the error label, if that failed."""
        if self._store.read_only:
            self._show_error(tr("functionkeys.readonly"))
            return False
        problems = self._problems(new)
        if problems:
            self._show_error("\n".join(problems))
            return False
        previous = self._controller.function_keys
        try:
            self._controller.apply_function_keys(new)
        except ValueError as exc:
            self._show_error(str(exc))
            return False
        try:
            self._store.save(new)
        except (ValueError, OSError) as exc:
            try:
                self._controller.apply_function_keys(previous)
            except ValueError:
                log.exception("Funktionstasten: vorheriger Stand konnte nicht wiederhergestellt werden")
            self._show_error(f"{tr('functionkeys.err.save')}: {exc}")
            return False
        self._keys = new
        self._fill_list()
        self._update_notice()
        return True

    def _save(self) -> None:
        slot = self._editor_slot()
        if self._commit(self._keys.replace_slot(slot)):
            self._load_editor(slot.slot)
            self._toast.show(tr("functionkeys.saved", name=slot.name))

    def _reset(self) -> None:
        saved = self._keys.slot(self._number)
        if saved.is_default():  # nothing stored: dropping the editor changes is the whole reset
            self._load_editor(self._number)
            return
        if not messagebox.askyesno(
            tr("functionkeys.reset.title"), tr("functionkeys.reset.confirm", name=saved.name), parent=self
        ):
            return
        if self._commit(self._keys.reset_slot(self._number)):
            self._load_editor(self._number)
            self._toast.show(tr("functionkeys.reset.done", name=saved.name))

    def _test(self) -> None:
        number = self._number
        slot = self._keys.slot(number)
        self._toast.show(tr("functionkeys.test.wait", name=slot.name, s=TEST_DELAY_MS // 1000))
        self._later("test", TEST_DELAY_MS, lambda: self._run_test(number))

    def _run_test(self, number: int) -> None:
        reason = self._controller.run_slot(number)
        name = self._keys.slot(number).name
        if reason is None:
            self._toast.show(tr("functionkeys.test.started", name=name))
        else:
            self._toast.show(reason, on=False)

    # notice

    def _update_notice(self) -> None:
        lines = list(self._load_warnings)
        lines += self._problems(self._keys)
        if lines:
            self._notice_text.configure(text="\n".join(lines))
            self._notice.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        else:
            self._notice.grid_remove()

    def refresh(self) -> None:
        """The settings may have changed (Morse key, shortcuts): the clash notice and the editor check follow."""
        self._update_notice()
        self._validate()
