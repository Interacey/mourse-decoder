"""Settings page. Every change applies immediately (live preview) and is saved.

An invalid input, e.g. a half-typed number, isn't applied but reported below the form.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .. import backup
from ..controller import MorseController
from ..dictionary import DictionaryManager
from ..function_keys import FunctionKeys, FunctionKeysStore
from ..i18n import available_languages, tr
from ..import_gate import gate
from ..settings import THEME_MODES, TYPING_DELAY_RANGE, Settings, SettingsStore, default_config_dir
from . import theme
from .form import parse_form
from .key_capture import CaptureBackend, KeyCaptureButton, ShortcutCaptureButton
from .theme import Button, Card, ScrollFrame, Switch, field_box, label, list_box

PREVIEW_LINES = 12  # file names listed in the import confirmation
PREVIEW_SLOTS = 8   # macros listed in the import confirmation
WORKER_POLL_MS = 50  # how often the Tk thread looks for the result of a backup job

log = logging.getLogger(__name__)

_TIMING_FIELDS = (
    ("dot_threshold_ms", "timing.dot"),
    ("letter_gap_ms", "timing.letter"),
    ("word_gap_ms", "timing.word"),
    ("typing_delay_ms", "timing.typing"),
)
# (from, to, step) of the number field; the others take any value up to 15000 and let validation report
_SPINBOX = {"typing_delay_ms": (*TYPING_DELAY_RANGE, 5)}
TYPING_DELAY_MS = 450  # apply number fields once typing pauses


class BackupDialogs:
    """The file and message dialogs of the backup buttons; tests pass a fake."""

    def __init__(self, parent: tk.Misc) -> None:
        self._parent = parent

    def save_path(self, title: str, initialfile: str) -> str:
        return filedialog.asksaveasfilename(
            parent=self._parent, title=title, initialfile=initialfile, defaultextension=".zip",
            filetypes=[(tr("backup.file.zip"), "*.zip")],
        )

    def open_path(self, title: str) -> str:
        return filedialog.askopenfilename(
            parent=self._parent, title=title, filetypes=[(tr("backup.file.zip"), "*.zip"), (tr("file.all"), "*.*")]
        )

    def ask(self, title: str, text: str) -> bool:
        return messagebox.askyesno(title, text, parent=self._parent)

    def choose(self, title: str, text: str) -> bool | None:
        """Yes (True), No (False) or Cancel (None); the text says what each one does."""
        return messagebox.askyesnocancel(title, text, parent=self._parent, icon="warning")

    def info(self, title: str, text: str) -> None:
        messagebox.showinfo(title, text, parent=self._parent)

    def error(self, title: str, text: str) -> None:
        messagebox.showerror(title, text, parent=self._parent)


class SettingsPanel(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        controller: MorseController,
        dictionaries: DictionaryManager,
        apply_settings: Callable[[Settings], None],
        capture_backend: CaptureBackend | None = None,
        backup_dialogs: BackupDialogs | None = None,
        config_dir: Path | None = None,
    ) -> None:
        super().__init__(master, bg=theme.P.bg)
        # the window, not this page: a language change rebuilds the pages while a dialog may still be open
        self._backup_dialogs = backup_dialogs or BackupDialogs(self.winfo_toplevel())
        self._backup_running = False  # a backup job is on its way: buttons off, no settings changes
        self._closed = False
        self._holds_gate = False  # this page has the import gate: other saves are refused until _finish
        self._job_running = False  # the worker thread is still inside its job
        self._worker_job: str | None = None
        self._backup_buttons: list[Button] = []
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._config_dir = Path(config_dir) if config_dir else default_config_dir()
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

        self._separate_dash = tk.BooleanVar()
        self._separate_dash.trace_add("write", lambda *_: self._changed())
        Switch(card.add_row(tr("settings.dash"), tr("settings.dash.hint")), self._separate_dash).pack()

        dash_row = card.add_row(tr("settings.dash_key"), tr("settings.dash_key.hint"))
        self._dash_button = KeyCaptureButton(
            dash_row, controller.settings.dash_trigger, on_change=lambda _name: self._changed(),
            on_hint=self._show_key_hint, backend=capture_backend,
        )
        self._dash_button.pack()

        self._block = tk.BooleanVar()
        self._block.trace_add("write", lambda *_: self._changed())
        Switch(card.add_row(tr("settings.block"), tr("settings.block.hint")), self._block).pack()

        shortcut_row = card.add_row(tr("settings.shortcut"), tr("settings.shortcut.hint"))
        self._shortcut_button = ShortcutCaptureButton(
            shortcut_row, controller.settings.block_shortcut, on_change=lambda _spec: self._changed(),
            on_hint=self._show_key_hint, backend=capture_backend,
        )
        self._shortcut_button.pack()

        undo_row = card.add_row(tr("settings.undo_shortcut"), tr("settings.undo_shortcut.hint"))
        self._undo_button = ShortcutCaptureButton(
            undo_row, controller.settings.undo_shortcut, on_change=lambda _spec: self._changed(),
            on_hint=self._show_key_hint, backend=capture_backend,
        )
        self._undo_button.pack()

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
            label(holder, tr("unit.ms"), muted=True).pack(side="right", padx=(8, 0))
            low, high, step = _SPINBOX.get(key, (0, 15000, 50))
            box, _ = field_box(
                holder, ttk.Spinbox, textvariable=var, from_=low, to=high, increment=step, width=7
            )
            box.configure(width=110, height=40)
            box.pack(side="right")
            self._vars[key] = var

        # the user's own list of alphabets, and the shortcut that switches through it
        self._section(8, "settings.section.cycle")
        cycle = Card(body)
        cycle.grid(row=9, column=0, sticky="ew")
        self._cycle_button = ShortcutCaptureButton(
            cycle.add_row(tr("settings.cycle_shortcut"), tr("settings.cycle_shortcut.hint")),
            controller.settings.cycle_shortcut, on_change=lambda _spec: self._changed(),
            on_hint=self._show_key_hint, backend=capture_backend,
        )
        self._cycle_button.pack()
        self._cycle_ids: list[str] = []  # alphabet id of each list row
        box, self._cycle_list = field_box(
            cycle.add_row(tr("settings.cycle_list"), tr("settings.cycle_list.hint")),
            list_box, selectmode="multiple", height=6, width=28,
        )
        box.configure(width=300, height=150)
        box.pack()
        self._cycle_list.bind("<<ListboxSelect>>", lambda _e: self._changed())

        self._error = tk.Label(body, bg=theme.P.bg, fg=theme.P.danger, font=theme.font(9), anchor="w", justify="left")
        self._error.grid(row=10, column=0, sticky="w", padx=8, pady=(8, 0))

        # backup of all user files as one zip
        self._section(11, "backup.section")
        saves = Card(body)
        saves.grid(row=12, column=0, sticky="ew")
        for key, command in (("export", self._export_backup), ("import", self._import_backup)):
            button = Button(saves.add_row(tr(f"backup.{key}"), tr(f"backup.{key}.hint")), tr(f"backup.{key}.button"), command)
            button.pack()
            self._backup_buttons.append(button)

        self.refresh()

    def _section(self, row: int, key: str) -> None:
        label(self._body, tr(key), size=9, weight="bold", muted=True).grid(
            row=row, column=0, sticky="w", padx=8, pady=(16 if row > 1 else 4, 8)
        )

    def _combo(self, parent: tk.Misc, variable: tk.StringVar, values: list[str] | None = None) -> ttk.Combobox:
        box, combo = field_box(parent, ttk.Combobox, textvariable=variable, state="readonly", width=30, values=values or [])
        box.configure(width=300, height=40)
        box.pack()
        combo.bind("<<ComboboxSelected>>", lambda _e: self._changed())
        return combo

    def _show_key_hint(self, text: str) -> None:
        if text:
            self._key_hint.grid(row=3, column=0, sticky="w", padx=8, pady=(8, 0))
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
            self._separate_dash.set(current.separate_dash_key)
            self._dash_button.set_trigger(current.dash_trigger)
            self._dash_button.set_enabled(current.separate_dash_key)
            self._block.set(current.block_input)
            self._notify.set(current.notifications)
            self._shortcut_button.set_shortcut(current.block_shortcut)
            self._undo_button.set_shortcut(current.undo_shortcut)

            infos = self._dictionaries.available()
            self._alphabet_by_label = {a.name: a.id for a in infos}
            self._alphabet_box.configure(values=list(self._alphabet_by_label))
            self._alphabet_var.set(next((a.name for a in infos if a.id == current.alphabet), current.alphabet))
            self._cycle_button.set_shortcut(current.cycle_shortcut)
            self._cycle_ids = [a.id for a in infos]
            self._cycle_list.delete(0, "end")
            for i, info in enumerate(infos):
                self._cycle_list.insert("end", f"  {info.name}")
                if info.id in current.cycle_alphabets:
                    self._cycle_list.selection_set(i)

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
        if self._loading or self._busy or self._backup_running:  # an import writes settings.json itself
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
        self._dash_button.set_enabled(self._separate_dash.get())
        settings = settings.replace(
            separate_dash_key=self._separate_dash.get(),
            dash_trigger=self._dash_button.trigger,
            enabled=self._enabled.get(),
            block_input=self._block.get(),
            notifications=self._notify.get(),
            block_shortcut=self._shortcut_button.shortcut,
            cycle_shortcut=self._cycle_button.shortcut,
            undo_shortcut=self._undo_button.shortcut,
            cycle_alphabets=tuple(self._cycle_ids[i] for i in self._cycle_list.curselection()),
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
            self._busy = False
            self.refresh()  # reset the form to what is actually active; this clears the error label
            self._error.configure(text=f"{tr('err.apply')}: {exc}")
        finally:
            self._busy = False

    # backup: validation, export and import run in a worker thread, the dialogs and everything on screen here

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            self._closed = True
            if not self._job_running:  # else the worker releases the gate when it is done: it is still writing
                self._release_gate()
            if self._worker_job is not None:
                self.after_cancel(self._worker_job)
                self._worker_job = None

    def _set_backup_running(self, running: bool) -> None:
        self._backup_running = running
        for button in self._backup_buttons:
            button.set_enabled(not running)

    def _start(self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]) -> None:
        """Run ``work`` in a thread; ``done(result, error)`` then runs here, on the Tk thread.

        The thread only calls ``work`` and puts the outcome in a queue, it never touches Tk. The buttons stay
        off until a job ends with ``_finish`` (a job may start the next one from ``done``).
        """
        self._set_backup_running(True)
        self._job_running = True
        outcome: queue.Queue[tuple[Any, BaseException | None]] = queue.Queue()

        def run() -> None:
            try:
                outcome.put((work(), None))
            except BaseException as exc:  # noqa: BLE001 - handed to done(), which shows it
                outcome.put((None, exc))
            finally:
                self._job_running = False
                if self._closed:  # the page was destroyed while the job ran: nobody is left to release the gate
                    self._release_gate()

        threading.Thread(target=run, name="backup", daemon=True).start()
        self._poll(outcome, done)

    def _poll(self, outcome: queue.Queue, done: Callable[[Any, BaseException | None], None]) -> None:
        if self._closed:
            return
        try:
            result, error = outcome.get_nowait()
        except queue.Empty:
            self._worker_job = self.after(WORKER_POLL_MS, self._poll, outcome, done)
            return
        self._worker_job = None
        try:
            done(result, error)
        except Exception as exc:  # noqa: BLE001 - a traceback in a Tk callback would leave the page stuck
            log.exception("Backup: Fehler beim Abschluss")
            self._finish()
            self._show_error("backup.err.import", exc)

    def _finish(self) -> None:
        self._release_gate()
        if not self._closed:
            self._set_backup_running(False)

    def _release_gate(self) -> None:
        if self._holds_gate:
            self._holds_gate = False
            gate.end()

    def _error_text(self, error: BaseException) -> str:
        """The message of a failed job in the current language; a restore point is always named."""
        if isinstance(error, backup.BackupError):
            key = f"backup.err.{error.code}"
            text = tr(key, **error.params)
            if text == key:  # a code without translation, or free text: the English message of the error
                text = str(error)
        else:
            text = tr("backup.err.unexpected", error=f"{type(error).__name__}: {error}")
        point = getattr(error, "restore_point", None)
        if point is not None and str(point) not in text:
            text += "\n\n" + tr("backup.restore_point", path=point)
        return text

    def _show_error(self, title_key: str, error: BaseException) -> None:
        try:
            self._backup_dialogs.error(tr(title_key), self._error_text(error))
        except Exception:  # noqa: BLE001 - the dialog itself failed, e.g. the window is gone
            log.exception("Backup: Fehlerdialog nicht anzeigbar")

    def _export_backup(self) -> None:
        if self._backup_running:
            return
        try:
            self._export_dialogs()
        except Exception as exc:  # noqa: BLE001 - last resort, never a traceback in the callback
            log.exception("Backup-Export")
            self._finish()
            self._show_error("backup.err.export", exc)

    def _export_dialogs(self) -> None:
        dialogs = self._backup_dialogs
        target = dialogs.save_path(tr("backup.dlg.export"), f"mourse-backup-{datetime.now():%Y%m%d}.zip")
        if not target:
            return
        with_telecode = (self._config_dir / backup.TELECODE_FILE).is_file() and dialogs.ask(
            tr("backup.export"), tr("backup.telecode.ask")
        )
        config = self._config_dir

        def done(manifest: Any, error: BaseException | None) -> None:
            self._finish()
            if error is not None:
                self._show_error("backup.err.export", error)
            else:
                dialogs.info(tr("backup.export"), tr("backup.export.done", count=len(manifest["files"]), path=target))

        self._start(lambda: backup.export_bundle(config, Path(target), include_telecode=with_telecode), done)

    def _import_backup(self) -> None:
        if self._backup_running:
            return
        try:
            source = self._backup_dialogs.open_path(tr("backup.dlg.import"))
            if source:
                self._inspect(Path(source), ())
        except Exception as exc:  # noqa: BLE001
            log.exception("Backup-Import")
            self._finish()
            self._show_error("backup.err.import", exc)

    def _inspect(self, source: Path, exclude: tuple[str, ...]) -> None:
        config, check = self._config_dir, self._controller.check_shortcut

        def work() -> backup.Preview:
            return backup.inspect_bundle(source, config, exclude=exclude, check_shortcut=check)

        self._start(work, lambda preview, error: self._inspected(source, exclude, preview, error))

    def _inspected(self, source: Path, exclude: tuple[str, ...], preview: backup.Preview | None, error: BaseException | None) -> None:
        dialogs = self._backup_dialogs
        if error is not None:
            # the one file that can be left out: offer the rest of the backup without it
            if (
                getattr(error, "path", None) == backup.FUNCTION_KEYS_FILE
                and backup.FUNCTION_KEYS_FILE not in exclude
                and dialogs.ask(tr("backup.err.import"), self._error_text(error) + "\n\n" + tr("backup.import.skipkeys.ask"))
            ):
                self._inspect(source, (*exclude, backup.FUNCTION_KEYS_FILE))
                return
            self._finish()
            if getattr(error, "path", None) != backup.FUNCTION_KEYS_FILE or backup.FUNCTION_KEYS_FILE in exclude:
                self._show_error("backup.err.import", error)
            return
        assert preview is not None
        if not preview.count("add") and not preview.count("replace"):
            self._finish()
            dialogs.info(tr("backup.import"), tr("backup.import.nothing"))
            return
        text = self._confirm_text(preview)
        if preview.slots:
            choice = dialogs.choose(tr("backup.import"), text + "\n\n" + tr("backup.import.choice"))
            if choice is None:
                self._finish()
                return
            if choice is False:
                exclude = (*exclude, backup.FUNCTION_KEYS_FILE)
        elif not dialogs.ask(tr("backup.import"), text):
            self._finish()
            return
        config, check, digest = self._config_dir, self._controller.check_shortcut, preview.sha256

        def work() -> backup.ImportResult:
            return backup.import_bundle(source, config, exclude=exclude, expected_sha256=digest, check_shortcut=check)

        if not gate.begin():  # a second import cannot be running, but a refused start must not leave the page stuck
            self._finish()
            self._show_error("backup.err.import", backup.BackupError("busy"))
            return
        self._holds_gate = True
        self._start(work, self._imported)

    def _confirm_text(self, preview: backup.Preview) -> str:
        changes = [e for e in preview.entries if e.status != "unchanged"]
        listed = "\n".join(f"{tr('backup.status.' + e.status)}: {e.label}" for e in changes[:PREVIEW_LINES])
        if len(changes) > PREVIEW_LINES:
            listed += "\n" + tr("backup.import.more", count=len(changes) - PREVIEW_LINES)
        blocks: list[str] = []
        if preview.slots:
            lines = [
                tr("backup.import.slot", slot=s.slot, trigger=s.trigger or "-", macro=s.macro,
                   risks=(" [" + ", ".join(s.risks) + "]") if s.risks else "")
                for s in preview.slots[:PREVIEW_SLOTS]
            ]
            if len(preview.slots) > PREVIEW_SLOTS:
                lines.append(tr("backup.import.more", count=len(preview.slots) - PREVIEW_SLOTS))
            warning = tr("backup.import.warn.macros", count=len(preview.slots))
            if preview.risky:
                warning += " " + tr("backup.import.warn.risky", commands=", ".join(sorted({r for s in preview.slots for r in s.risks})))
            blocks.append(warning + "\n" + "\n".join(lines))
        if preview.warnings:
            blocks.append(tr("backup.import.notes", notes="\n".join(backup.display_text(w, 200) for w in preview.warnings[:5])))
        return tr(
            "backup.import.confirm",
            add=preview.count("add"), replace=preview.count("replace"), unchanged=preview.count("unchanged"),
            version=backup.display_text(str(preview.manifest.get("app_version", "?")), 40),
            created=backup.display_text(str(preview.manifest.get("created_utc", "?")), 40),
            files=listed, extra="".join(block + "\n\n" for block in blocks),
        )

    def _imported(self, result: backup.ImportResult | None, error: BaseException | None) -> None:
        dialogs = self._backup_dialogs
        if error is not None:
            self._finish()
            self._show_error("backup.err.import", error)
            return
        assert result is not None
        problem, restart = self._load_into_running_app(result.written)
        self._finish()
        text = (
            tr("backup.import.done", path=result.restore_point) if result.restore_point else tr("backup.import.done.nopoint")
        )
        if restart:
            text += "\n\n" + tr("backup.import.restart", what=", ".join(tr(f"backup.restart.{key}") for key in restart))
        if problem:
            text += "\n\n" + tr("backup.import.apply_failed", reason=problem)
        dialogs.info(tr("backup.import"), text)

    def _load_into_running_app(self, written: tuple[str, ...]) -> tuple[str, list[str]]:
        """Make the program use the imported files now, through the same paths a change in the UI takes.

        Without this the running program keeps the old state in memory and its next save (tray menu,
        settings form, function key editor) writes it over the import. Returns (reason if something could not
        be loaded, keys of what only a restart reads). The settings go through ``apply_settings`` of the app
        (which also saves them and refreshes every page); the function keys are cleared first, because the
        old keys may clash with the new Morse key and the other way round.
        """
        controller, config = self._controller, self._config_dir
        restart = [key for key, folder in (("languages", "languages/"),) if any(w.startswith(folder) for w in written)]
        if backup.TELECODE_FILE in written:
            restart.append("telecode")
        previous = controller.function_keys
        keys_loaded = False
        try:
            settings = SettingsStore(config).load() if backup.SETTINGS_FILE in written else controller.settings
            keys = None
            if backup.FUNCTION_KEYS_FILE in written:
                keys, _warnings = FunctionKeysStore(config).load(controller.check_shortcut)
                controller.apply_function_keys(FunctionKeys())
                keys_loaded = True
            with gate.allow():  # the saves of the import itself pass the gate
                self._apply(settings)  # the app also refreshes its pages; the function keys page reloads its file
            self.refresh()  # in any case: a form that still shows the old values would save them with the next change
            if keys is not None:
                controller.apply_function_keys(keys)
            if any(w.startswith(("alphabets/", "smileys/")) or w == backup.TELECODE_FILE for w in written):
                controller.reload_alphabet()  # the active alphabet or smiley set may be one of the files
        except Exception as exc:  # noqa: BLE001 - the files are in place, the running program is what is left
            log.exception("Backup: Import konnte nicht geladen werden")
            if keys_loaded:
                try:
                    controller.apply_function_keys(previous)
                except Exception:  # noqa: BLE001
                    log.exception("Backup: alte Funktionstasten nicht wiederhergestellt")
            return str(exc), restart
        return "", restart
