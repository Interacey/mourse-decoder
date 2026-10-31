"""Update page: look at the latest release, install it, or remove the program completely."""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from tkinter import messagebox

from .. import __version__, updater
from ..i18n import tr
from ..settings import default_config_dir
from . import theme
from .theme import Button, Card, boxed, label, text_box

log = logging.getLogger(__name__)

POLL_MS = 100


class UpdatePanel(tk.Frame):
    def __init__(self, master: tk.Misc, on_quit: Callable[[], None]) -> None:
        super().__init__(master, bg=theme.P.bg)
        self._on_quit = on_quit
        self._release: updater.Release | None = None
        self._results: queue.Queue[Callable[[], None]] = queue.Queue()  # worker threads must not touch tkinter
        self._busy = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        card = Card(self)
        card.grid(row=0, column=0, sticky="ew")
        label(card.add_row(tr("update.installed")), __version__, weight="bold").pack()
        self._latest = label(card.add_row(tr("update.latest")), "–", weight="bold")
        self._latest.pack()

        label(self, tr("update.notes"), size=9, weight="bold", muted=True).grid(row=1, column=0, sticky="w", pady=(18, 6))
        box, self._notes = boxed(self, text_box, pad=4, height=6, wrap="word", undo=False, state="disabled", font=theme.font(10))
        box.grid(row=2, column=0, sticky="nsew")

        self._status = tk.StringVar()
        tk.Label(
            self, textvariable=self._status, bg=theme.P.bg, fg=theme.P.muted, font=theme.font(9), anchor="w",
            justify="left", wraplength=600,
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

        buttons = tk.Frame(self, bg=theme.P.bg)
        buttons.grid(row=4, column=0, sticky="w", pady=(10, 0))
        self._check_btn = Button(buttons, tr("update.check"), self._check, min_width=170)
        self._install_btn = Button(buttons, tr("update.install"), self._install, variant="primary", min_width=170)
        self._check_btn.pack(side="left", padx=(0, 6))
        self._install_btn.pack(side="left")
        self._install_btn.set_enabled(False)

        label(self, tr("uninstall.section"), size=9, weight="bold", muted=True).grid(row=5, column=0, sticky="w", pady=(24, 6))
        removal = Card(self)
        removal.grid(row=6, column=0, sticky="ew")
        Button(removal.add_row(tr("uninstall.title"), tr("uninstall.hint")), tr("uninstall.button"), self._uninstall).pack()

    def refresh(self) -> None:
        """Nothing to reload: the page only talks to GitHub when a button is pressed."""

    # work runs in a thread, its result comes back through the queue and is shown here
    def _work(self, job: Callable[[], Callable[[], None]], status: str) -> None:
        if self._busy:
            return
        self._busy = True
        self._status.set(status)
        self._check_btn.set_enabled(False)
        self._install_btn.set_enabled(False)

        def run() -> None:
            try:
                self._results.put(job())
            except Exception as exc:  # noqa: BLE001 - never leave the page stuck on "busy"
                if not isinstance(exc, updater.UpdateError):
                    log.exception("Update")
                message = str(exc)  # `exc` is gone once this block ends, the UI thread runs the lambda later
                self._results.put(lambda: self._failed(message))

        threading.Thread(target=run, daemon=True, name="update").start()
        self.after(POLL_MS, self._poll)

    def _poll(self) -> None:
        try:
            show = self._results.get_nowait()
        except queue.Empty:
            self.after(POLL_MS, self._poll)
            return
        self._busy = False
        self._check_btn.set_enabled(True)
        show()

    def _failed(self, message: str) -> None:
        self._status.set(tr("update.error", msg=message))
        self._install_btn.set_enabled(self._release is not None and updater.is_newer(self._release.tag))

    def _check(self) -> None:
        def job() -> Callable[[], None]:
            release = updater.fetch_latest()
            return lambda: self._show_release(release)

        self._work(job, tr("update.checking"))

    def _show_release(self, release: updater.Release) -> None:
        self._release = release
        newer = updater.is_newer(release.tag)
        self._latest.configure(text=release.tag)
        self._notes.configure(state="normal")
        self._notes.delete("1.0", "end")
        self._notes.insert("1.0", release.notes)
        self._notes.configure(state="disabled")
        self._status.set(tr("update.available", tag=release.tag) if newer else tr("update.current"))
        self._install_btn.set_enabled(newer)

    def _install(self) -> None:
        release = self._release
        if release is None:
            return
        if not updater.can_self_update():
            self._status.set(tr("update.only_exe"))
            webbrowser.open(release.page)
            return

        def job() -> Callable[[], None]:
            updater.install(release)
            return self._on_quit  # the new exe replaces this one as soon as it has ended

        self._work(job, tr("update.downloading"))

    def _uninstall(self) -> None:
        folder = default_config_dir()
        if not messagebox.askyesno(tr("uninstall.confirm.title"), tr("uninstall.confirm", folder=folder), parent=self):
            return
        try:
            updater.uninstall(folder)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(tr("uninstall.confirm.title"), tr("uninstall.error", msg=exc), parent=self)
            return
        self._on_quit()
