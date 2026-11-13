"""Tray icon (pystray) and the bridge to the tkinter main loop.

Three threads are involved: tkinter on the main thread, pystray menu callbacks on
the tray thread and engine callbacks on the Rust worker. tkinter isn't thread-safe,
so other threads queue a function that the main thread runs every 250 ms.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox

import pystray

from ..controller import MorseController
from ..dictionary import AlphabetError, DictionaryManager
from ..i18n import set_language, tr
from ..import_gate import gate
from ..settings import Settings, SettingsStore
from ..telecode import TelecodeError
from ..triggers import binding_label
from . import theme, tkfinalizers
from .icon import create_icon
from .main_window import MainWindow
from .splash import Splash
from .toast import Toast

log = logging.getLogger(__name__)

POLL_INTERVAL_MS = 250
# What settings.json says and the program does not always run: the switches the on/off shortcut flips at run time,
# and the alphabet, which falls back to the default in memory when the named one cannot be loaded (at the start and
# after an import). The tray keeps the file's values (``saved_fields``) and writes them back unless the user
# changes them, so a pause or a fallback never rewrites the file.
SAVED_FIELDS = ("enabled", "block_input", "alphabet", "smileys")


def saved_fields(store: SettingsStore) -> dict[str, object]:
    """The values of ``SAVED_FIELDS`` that settings.json holds now (not what the controller runs)."""
    on_disk = SettingsStore(store.directory).load()  # a new store: the warnings of ``store`` stay as they are
    return {name: getattr(on_disk, name) for name in SAVED_FIELDS}


BALLOON_UNITS = 255  # NOTIFYICONDATA.szInfo holds 256 UTF-16 units including the end mark
BALLOON_TITLE_UNITS = 63  # szInfoTitle holds 64


def fit_balloon(message: str, limit: int = BALLOON_UNITS) -> str:
    """``message`` cut to ``limit`` UTF-16 units so that a tray balloon accepts it (pystray raises "string too long").

    Counted by code point (a character above U+FFFF is two units) and never encoded, so text from a file with an
    unpaired surrogate cannot raise here: such a character is shown as U+FFFD.
    """
    out, used = [], 0
    for char in message:
        units = 2 if ord(char) > 0xFFFF else 1
        out.append("\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char)
        used += units
    if used <= limit:
        return "".join(out)
    kept, used = [], 1  # one unit for the ellipsis
    for char in out:
        units = 2 if ord(char) > 0xFFFF else 1
        if used + units > limit:
            break
        kept.append(char)
        used += units
    return "".join(kept) + "…"


def take_over_imported(controller: MorseController, settings: Settings) -> Settings:
    """Apply the settings of an imported ``settings.json``; returns what the controller really runs.

    The same rules as at the start of the program (``app.build_controller``): an alphabet that cannot be loaded
    (it is not in the backup, telecode.sqlite is missing) is replaced by the default in memory, and a smiley set
    that is not there leaves the name in the settings and the smileys off until it appears. The rest of the file
    takes effect; the file on disk keeps what the import wrote. Anything else (e.g. a shortcut clash) is raised.
    """
    try:
        controller.apply_settings(settings, lenient_smileys=True)
        return settings
    except (AlphabetError, TelecodeError) as first:
        fallback = settings.replace(alphabet=Settings().alphabet)
        if fallback == settings:
            raise
        log.warning("Import: Alphabet '%s' nicht ladbar (%s), Standard wird verwendet", settings.alphabet, first)
        controller.apply_settings(fallback, lenient_smileys=True)
        return fallback


class TrayApp:
    _quit_pending = False  # Quit was asked for while a backup import was writing: done when it is over

    def __init__(
        self,
        controller: MorseController,
        dictionaries: DictionaryManager,
        store: SettingsStore,
        start_hidden: bool = False,
        hook_error: Callable[[], str | None] | None = None,
    ) -> None:
        self._hook_error = hook_error  # the native ``hook_error``: text once the keyboard hook has died
        self._hook_error_shown = False
        self._controller = controller
        self._dictionaries = dictionaries
        self._store = store
        # what settings.json says about the two switches the on/off shortcut flips at run time
        self._saved_flags = saved_fields(store)
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
            on_quit=self.quit,
            config_dir=store.directory,
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
        tkfinalizers.drain()
        while True:
            try:
                func = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func()
            except Exception:  # noqa: BLE001
                log.exception("Fehler in UI-Aufgabe")
        try:
            # one failing poll must not stop the others, and the loop must be scheduled again in any case
            if self._quit_pending and not gate.busy():
                self._quit_pending = False
                self.quit()
            polls = (
                self._poll_block,
                self._poll_cycle,
                self._poll_undo,
                self._poll_function_keys,
                self._poll_hook,
                self._poll_calibration,
            )
            for poll in polls:
                if poll in (self._poll_block, self._poll_cycle) and gate.blocked():
                    continue  # a backup import is running: the key presses stay pending until it is done
                try:
                    poll()
                except Exception:  # noqa: BLE001
                    log.exception("Abfrage %s fehlgeschlagen", poll.__name__)
        finally:
            self._root.after(POLL_INTERVAL_MS, self._drain_queue)

    def _poll_calibration(self) -> None:
        """A measuring that nobody ended stops after its time limit, even if no key is pressed."""
        self._controller.check_calibration_timeout()

    def _poll_hook(self) -> None:
        """Once the keyboard hook has died, Morse input is gone: say so one time."""
        if self._hook_error is None or self._hook_error_shown:
            return
        error = self._hook_error()
        if error is None:
            return
        self._hook_error_shown = True
        log.error("Tastatur-Hook ausgefallen: %s", error)
        self._icon.notify(fit_balloon(error), fit_balloon(tr("tray.error"), BALLOON_TITLE_UNITS))
        self._toast.show(error, False)

    def _poll_block(self) -> None:
        """The hook's shortcut can turn Morse input on or off; mirror that into the settings."""
        toggled = self._controller.poll_active_toggle()
        if toggled is None:
            return
        try:
            self._apply(toggled, runtime_only=True)
        except Exception:  # noqa: BLE001
            log.exception("Morse-Eingabe konnte nicht umgeschaltet werden")
            return
        # the shortcut works from any app, so say what happened (quietly, and only if wanted)
        if toggled.notifications:
            self._toast.show(tr("status.active") if toggled.enabled else tr("status.paused"), toggled.enabled)
    def _poll_function_keys(self) -> None:
        """Start the macros of function keys that were pressed; they run on their own threads."""
        try:
            self._controller.poll_function_keys()
        except Exception:  # noqa: BLE001 - polling must never stop the UI loop
            log.exception("Funktionstasten konnten nicht abgefragt werden")

    def _poll_cycle(self) -> None:
        """The cycle shortcut switches to the next alphabet of the user's list and says which one is active."""
        presses = self._controller.poll_cycle()
        if not presses:
            return
        infos = {a.id: a.name for a in self._dictionaries.available()}
        target = self._controller.settings.cycle_target(infos, presses)
        if target is None:
            self._toast.show(tr("cycle.empty"), False)
            return
        alphabet, position, total = target
        try:
            self._apply(self._controller.settings.replace(alphabet=alphabet), chosen=("alphabet",))
        except Exception as exc:  # noqa: BLE001 - e.g. a broken alphabet file
            log.exception("Umschalten auf %s fehlgeschlagen", alphabet)
            self._toast.show(f"{tr('err.apply')}: {exc}", False)
            return
        # always shown, even with "show notices" off: it is the only sign that the shortcut worked
        self._toast.show(tr("cycle.toast", name=infos[alphabet], pos=position, total=total))

    def _poll_undo(self) -> None:
        """The undo shortcut deletes the last output; any number of presses since the last tick is one undo."""
        if self._controller.poll_undo():
            self._undo()

    def _undo(self) -> None:
        """Undo on a thread of its own: it may wait for held modifier keys, which must not freeze the window."""
        threading.Thread(target=self._run_undo, name="undo", daemon=True).start()

    def _run_undo(self) -> None:
        try:
            result = self._controller.undo_last_output()
        except Exception as exc:  # noqa: BLE001 - e.g. the injector failed
            log.exception("Rückgängig fehlgeschlagen")
            message = f"{tr('tray.error')}: {exc}"
            self.call_in_ui(lambda: self._toast.show(message, False))
            return
        text = tr("undo.done", n=result.deleted) if result.deleted else tr(f"undo.{result.reason}")
        # always shown, even with "show notices" off: the shortcut works from any app and this is the only reply
        self.call_in_ui(lambda: self._toast.show(text, bool(result.deleted)))

    def on_symbol(self, symbol: str) -> None:
        self.call_in_ui(lambda: self._window.add_symbol(symbol))

    def on_timing(self, kind: str, ms: int) -> None:
        self.call_in_ui(lambda: self._window.add_timing(kind, ms))

    def on_calibration_ended(self, reason: str) -> None:
        self.call_in_ui(lambda: self._window.calibration_ended(reason))

    def on_letter(self, sequence: str, text: str | None) -> None:
        shown = text if text is not None else "?"

        def update() -> None:
            self._set_title(f"{self._tooltip()}\n{tr('tray.last', seq=sequence, text=shown)}")
            self._window.add_letter(sequence, text)

        self.call_in_ui(update)

    def on_error(self, message: str) -> None:
        shown = fit_balloon(message)
        self.call_in_ui(lambda: self._icon.notify(shown, fit_balloon(tr("tray.error"), BALLOON_TITLE_UNITS)))

    def _tooltip(self) -> str:
        s = self._controller.settings
        state = tr("tray.state.active") if s.enabled else tr("tray.state.paused")
        return tr("tray.tooltip", state=state, alphabet=s.alphabet, key=binding_label(s.trigger))

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
            pystray.MenuItem(tr("tray.undo"), lambda _i, _it: self.call_in_ui(self._undo)),
            pystray.MenuItem(tr("tray.settings"), lambda _i, _it: self.call_in_ui(self._open_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(tr("tray.quit"), lambda _i, _it: self.call_in_ui(self.quit)),
        )

    def _change(self, **changes: object) -> None:
        """Change from the tray menu: show errors instead of raising."""
        try:
            self._apply(self._controller.settings.replace(**changes), chosen=tuple(changes))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(tr("app.name"), f"{tr('err.apply')}:\n{exc}")

    def _to_save(
        self, settings: Settings, runtime_only: bool, chosen: tuple[str, ...] = ()
    ) -> tuple[Settings, dict[str, object]]:
        """The settings to write, and the saved values of ``SAVED_FIELDS`` after that.

        Those fields are written only when the user changed them (form, tray menu, cycle shortcut): a changed
        value, or one in ``chosen`` (picked explicitly, even if it equals what runs). A pause by the on/off
        shortcut (``runtime_only``) and any other save, made while paused or while a fallback alphabet runs,
        keep the values that are on disk, so the next start blocks again and the named alphabet is still named.
        """
        current = self._controller.settings
        saved = dict(self._saved_flags)
        if not runtime_only:
            for name in saved:
                if getattr(settings, name) != getattr(current, name) or name in chosen:
                    saved[name] = getattr(settings, name)
        return settings.replace(**saved), saved

    def _apply(self, settings: Settings, runtime_only: bool = False, chosen: tuple[str, ...] = ()) -> None:
        """Apply and save. Raises on errors, the caller shows them. ``chosen``: see ``_to_save``."""
        gate.check()  # ImportBusy while a backup import is writing the files
        if gate.loading():
            # the import loads the settings.json it wrote: that is what is saved, whatever the program runs
            wanted = settings
            if settings.enabled == self._saved_flags.get("enabled"):
                # the file says what it said before: not a change of the switch, so a pause by the shortcut stays
                wanted = settings.replace(enabled=self._controller.settings.enabled)
            live = take_over_imported(self._controller, wanted)
            to_save, saved = settings, {name: getattr(settings, name) for name in SAVED_FIELDS}
        else:
            to_save, saved = self._to_save(settings, runtime_only, chosen)
            self._controller.apply_settings(settings)
            live = settings
        self._store.save(to_save)
        self._saved_flags = saved
        self._icon.icon = create_icon(live.enabled)
        self._set_title(self._tooltip())
        self._icon.update_menu()
        self._window.sync()

    def _rebuild_menu(self) -> None:
        """Rebuild the alphabet submenu after files were added, deleted or imported; icon and tooltip follow."""
        self._icon.menu = self._build_menu()
        self._icon.icon = create_icon(self._controller.settings.enabled)
        self._set_title(self._tooltip())
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
        if gate.busy():  # a backup job (checking, the open question, the import) is not done: wait for it, then quit
            self._quit_pending = True
            self._toast.show(tr("backup.quit_wait"), False)
            return
        self._controller.stop()
        self._icon.stop()
        self._root.quit()
