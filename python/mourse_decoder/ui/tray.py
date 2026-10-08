"""Tray icon (pystray) and the bridge to the tkinter main loop.

Three threads are involved: tkinter on the main thread, pystray menu callbacks on
the tray thread and engine callbacks on the Rust worker. tkinter isn't thread-safe,
so other threads queue a function that the main thread runs every 250 ms.
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox

import pystray

from ..controller import MorseController
from ..dictionary import DictionaryManager
from ..i18n import set_language, tr
from ..settings import Settings, SettingsStore
from ..triggers import trigger_label
from . import theme
from .icon import create_icon
from .main_window import MainWindow
from .splash import Splash
from .toast import Toast

log = logging.getLogger(__name__)

POLL_INTERVAL_MS = 250


class TrayApp:
    def __init__(
        self,
        controller: MorseController,
        dictionaries: DictionaryManager,
        store: SettingsStore,
        start_hidden: bool = False,
    ) -> None:
        self._controller = controller
        self._dictionaries = dictionaries
        self._store = store
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self._root = tk.Tk()
        self._root.withdraw()  # shown after the splash screen
        set_language(controller.settings.language, store.directory / "languages")
        theme.apply_theme(self._root, controller.settings.theme)
        if not start_hidden:
            Splash(self._root, on_done=lambda: self._window.show())
            self._root.update()
        self._toast = Toast(self._root)
        self._window = MainWindow(
            self._root,
            controller,
            dictionaries,
            apply_settings=self._apply,
            on_alphabets_changed=self._rebuild_menu,
            on_hide=self._hide_window,
        )
        self._icon = pystray.Icon(
            "mourse-decoder",
            icon=create_icon(controller.settings.enabled),
            title=self._tooltip(),
            menu=self._build_menu(),
        )

    def call_in_ui(self, func: Callable[[], None]) -> None:
        """Callable from any thread."""
        self._ui_queue.put(func)

    def _drain_queue(self) -> None:
        while True:
            try:
                func = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func()
            except Exception:  # noqa: BLE001
                log.exception("Fehler in UI-Aufgabe")
        self._poll_block()
        self._root.after(POLL_INTERVAL_MS, self._drain_queue)

    def _poll_block(self) -> None:
        """The hook's shortcut can turn Morse input on or off; mirror that into the settings."""
        toggled = self._controller.poll_active_toggle()
        if toggled is None:
            return
        try:
            self._apply(toggled)
        except Exception:  # noqa: BLE001
            log.exception("Morse-Eingabe konnte nicht umgeschaltet werden")
            return
        # the shortcut works from any app, so say what happened (quietly, and only if wanted)
        if toggled.notifications:
            self._toast.show(tr("status.active") if toggled.enabled else tr("status.paused"), toggled.enabled)
    def on_symbol(self, symbol: str) -> None:
        self.call_in_ui(lambda: self._window.live_panel.add_symbol(symbol))

    def on_letter(self, sequence: str, text: str | None) -> None:
        shown = text if text is not None else "?"

        def update() -> None:
            self._set_title(f"{self._tooltip()}\n{tr('tray.last', seq=sequence, text=shown)}")
            self._window.live_panel.add_letter(sequence, text)

        self.call_in_ui(update)

    def on_error(self, message: str) -> None:
        self.call_in_ui(lambda: self._icon.notify(message, tr("tray.error")))

    def _tooltip(self) -> str:
        s = self._controller.settings
        state = tr("tray.state.active") if s.enabled else tr("tray.state.paused")
        return tr("tray.tooltip", state=state, alphabet=s.alphabet, key=trigger_label(s.trigger))

    def _set_title(self, title: str) -> None:
        # Windows caps tray tooltips at 127 characters
        self._icon.title = title[:127]

    def _build_menu(self) -> pystray.Menu:
        def alphabet_item(info_id: str, label: str) -> pystray.MenuItem:
            return pystray.MenuItem(
                label,
                lambda _icon, _item: self.call_in_ui(lambda: self._change(alphabet=info_id)),
                checked=lambda _item: self._controller.settings.alphabet == info_id,
                radio=True,
            )

        alphabets = [alphabet_item(info.id, info.name) for info in self._dictionaries.available()]
        return pystray.Menu(
            pystray.MenuItem(
                tr("tray.open"),
                lambda _icon, _item: self.call_in_ui(self._window.show),
                default=True,  # double-click on the icon opens the window
            ),
            pystray.MenuItem(
                tr("tray.active"),
                lambda _icon, _item: self.call_in_ui(
                    lambda: self._change(enabled=not self._controller.settings.enabled)
                ),
                checked=lambda _item: self._controller.settings.enabled,
            ),
            pystray.MenuItem(tr("tray.alphabet"), pystray.Menu(*alphabets)),
            pystray.MenuItem(tr("tray.settings"), lambda _i, _it: self.call_in_ui(self._open_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(tr("tray.quit"), lambda _i, _it: self.call_in_ui(self.quit)),
        )

    def _change(self, **changes: object) -> None:
        """Change from the tray menu: show errors instead of raising."""
        try:
            self._apply(self._controller.settings.replace(**changes))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(tr("app.name"), f"{tr('err.apply')}:\n{exc}")

    def _apply(self, settings: Settings) -> None:
        """Apply and save. Raises on errors, the caller shows them."""
        self._controller.apply_settings(settings)
        self._store.save(settings)
        self._icon.icon = create_icon(settings.enabled)
        self._set_title(self._tooltip())
        self._icon.update_menu()
        self._window.sync()

    def _rebuild_menu(self) -> None:
        """Rebuild the alphabet submenu after files were added or deleted."""
        self._icon.menu = self._build_menu()
        self._icon.update_menu()

    def _open_settings(self) -> None:
        self._window.show()
        self._window.show_tab("settings")

    def _hide_window(self) -> None:
        self._window.hide()

    def run(self) -> None:
        """Blocks until quit."""
        self._icon.run_detached()  # pystray runs in its own thread
        self._root.after(POLL_INTERVAL_MS, self._drain_queue)
        self._root.mainloop()

    def quit(self) -> None:
        self._controller.stop()
        self._icon.stop()
        self._root.quit()
