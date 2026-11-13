"""Entry point: wires everything together (the only place that builds concrete classes)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import desktop, updater
from ._native import load_native
from .controller import MorseController, NativeInjector
from .dictionary import AlphabetError, DictionaryManager
from .function_keys import FunctionKeysStore
from .i18n import set_language, tr
from .jsontext import clean_orphaned_scratch, display_text
from .settings import Settings, SettingsStore
from .telecode import TelecodeError

log = logging.getLogger("mourse_decoder")

FALLBACK_ALPHABET = "latin"
MUTEX_NAME = "Local\\MourseDecoder"
_ERROR_ALREADY_EXISTS = 183
_mutex_handles: list[int] = []  # kept open for the life of the process


class _ConsoleListener:
    """Output for --headless."""

    def on_symbol(self, symbol: str) -> None:
        print(symbol, end="", flush=True)

    def on_letter(self, sequence: str, text: str | None) -> None:
        print(f"  → {text if text is not None else '?'}", flush=True)

    def on_error(self, message: str) -> None:
        print(f"Fehler: {message}", file=sys.stderr, flush=True)


def build_controller(engine, injector, dictionaries, settings: Settings, listener=None):
    """Controller for ``settings``; if the alphabet cannot be loaded, the default one is used.

    Returns (controller, warnings). The settings file is not touched; only the running
    controller uses the fallback.
    """
    try:
        return MorseController(engine, injector, dictionaries, settings, listener=listener), []
    except (AlphabetError, TelecodeError) as exc:
        fallback = settings.replace(alphabet=FALLBACK_ALPHABET)
        warning = tr("startup.alphabet_fallback", id=settings.alphabet, msg=exc)
        return MorseController(engine, injector, dictionaries, fallback, listener=listener), [warning]


def deliver_warnings(show, warnings) -> None:
    """Show each startup warning with ``show``; a warning that cannot be shown never stops the start.

    The text of a warning can come from a file (a trigger in function_keys.json, an alphabet name): whatever it
    holds, the program starts, the others are shown and the failure goes to the log.
    """
    for warning in warnings:
        try:
            show(warning)
        except Exception:  # noqa: BLE001
            log.exception("Startwarnung konnte nicht angezeigt werden")


def settings_warnings(store: SettingsStore) -> list[str]:
    """What ``store.load`` changed in the settings on its own (an invalid id), in the program's language.

    The file is not touched here; the next save writes the replaced value, so this message comes first.
    """
    return [tr(key, **values) for key, values in store.warnings]


def acquire_single_instance(name: str = MUTEX_NAME) -> bool:
    """True if this is the only instance (named mutex, Windows only; other systems always True)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True  # no mutex, no guard: better a second instance than none
        _mutex_handles.append(handle)
        return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return True


def show_update_result(directory, show=None) -> None:
    """Tell once what became of the last update or uninstall; reading removes the result file."""
    message = updater.read_update_result(directory)
    if not message:
        return
    if show is None:
        from tkinter import messagebox

        show = messagebox.showwarning
    show(tr("update.result.title"), message)


def _tell_already_running() -> None:
    message = tr("app.already_running")
    print(message, file=sys.stderr, flush=True)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Mourse Decoder", 0x40)
        except (AttributeError, OSError):
            pass


def _tolerate_unencodable_output() -> None:
    """A redirected stdout may be cp1252 and could not print → or Japanese text; replace instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def _start_function_keys(controller: MorseController, directory, injector) -> list[str]:
    """Load function_keys.json and install the slots; returns the warnings to show."""
    keys, warnings = FunctionKeysStore(directory).load(injector.check_shortcut)
    try:
        controller.apply_function_keys(keys)
    except ValueError as exc:  # e.g. a slot trigger equals the Morse key: run without slots rather than not at all
        warnings.append(f"Funktionstasten nicht aktiv: {exc}")
    return warnings


class _PrintInjector:
    """For --dry-run: show instead of typing."""

    dry_run = True  # the controller does not read the clipboard for {Scrape} and types a placeholder

    def inject_text(self, text: str) -> None:
        print(f"[tippe {text!r}]", flush=True)

    def type_text(self, text: str) -> None:
        self.inject_text(text)

    def pastes(self, text: str) -> bool:
        return False

    def inject_backspaces(self, count: int) -> None:
        print(f"[lösche {count}]", flush=True)

    def press_shortcut(self, combo: str) -> None:
        print(f"[drücke {combo}]", flush=True)

    def check_shortcut(self, combo: str) -> None:
        pass  # shortcut: dry run accepts every combo, the real run checks it in Rust

    def foreground_title(self) -> str:
        return desktop.foreground_title()

    def clipboard_text(self) -> str | None:
        return "<Zwischenablage>"  # never print what may be a password

    def paste_files(self, paths: list[str]) -> None:
        print(f"[füge ein {paths!r}]", flush=True)

    def scroll(self, notches: int) -> None:
        print(f"[scrolle {notches:+d}]", flush=True)


def _prepare_windows() -> None:
    """Windowed builds have no console (stdout/stderr are None), and the taskbar should show
    our icon instead of grouping the window under python.exe."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))  # noqa: SIM115 - lives as long as the process
    _tolerate_unencodable_output()
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MourseDecoder.App")
        except (AttributeError, OSError):
            pass


def _exit_when_launcher_dies() -> None:
    """End this process when its launcher is killed (e.g. in the Task Manager).

    Both the PyInstaller exe and the venv python start the real app as a child of a launcher
    process with the same file name. Ending only the launcher would leave the child running with
    its keyboard hook, which could keep blocking the Morse key. The hook disappears with its
    process, so exiting here guarantees input is never blocked after the app was killed.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import threading
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        query_limited, synchronize = 0x1000, 0x00100000
        handle = kernel32.OpenProcess(query_limited | synchronize, False, os.getppid())
        if not handle:
            return
        buffer, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return
        if os.path.basename(buffer.value).lower() != os.path.basename(sys.executable).lower():
            return  # started from a shell or another program, which may keep running without us

        def wait() -> None:
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            os._exit(0)

        threading.Thread(target=wait, daemon=True, name="launcher-watchdog").start()
    except (AttributeError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    _prepare_windows()
    _exit_when_launcher_dies()
    parser = argparse.ArgumentParser(prog="mourse-decoder", description="Systemweiter Morsecode-Decoder")
    parser.add_argument("--headless", action="store_true", help="ohne Tray-Icon, Ausgabe in der Konsole")
    parser.add_argument("--dry-run", action="store_true", help="nicht in Anwendungen tippen, nur anzeigen")
    parser.add_argument("--minimized", action="store_true", help="Fenster nicht anzeigen, nur Tray-Icon")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if not args.dry_run and not acquire_single_instance():
        _tell_already_running()
        return 3

    try:
        native = load_native()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    store = SettingsStore()
    settings = store.load()
    try:  # check folders of a backup check that a killed process left behind (copies of the settings and macros)
        clean_orphaned_scratch()
    except Exception:  # noqa: BLE001 - housekeeping must never stop the program
        log.exception("Temporäre Backup-Ordner nicht aufgeräumt")
    set_language(settings.language, store.directory / "languages")  # startup warnings use it
    notices = settings_warnings(store)
    dictionaries = DictionaryManager(
        telecode_db=store.directory / "telecode.sqlite",
        user_directory=store.directory / "alphabets",
        user_smiley_directory=store.directory / "smileys",
    )
    engine = native.MorseEngine(trigger=settings.trigger)
    injector = _PrintInjector() if args.dry_run else NativeInjector(native)

    if args.headless:
        controller, startup_warnings = build_controller(engine, injector, dictionaries, settings, _ConsoleListener())
        controller.start()
        deliver_warnings(
            lambda warning: print(f"Warnung: {warning}", file=sys.stderr, flush=True),
            notices + startup_warnings + _start_function_keys(controller, store.directory, injector),
        )
        print(
            f"Morse-Decoder läuft (Taster: {display_text(settings.trigger)}, "
            f"Alphabet: {display_text(settings.alphabet)}). Strg+C beendet."
        )
        try:
            while True:
                time.sleep(0.1)
                controller.poll_function_keys()
                if (err := native.hook_error()) is not None:
                    print(err, file=sys.stderr)
                    return 1
        except KeyboardInterrupt:
            pass
        finally:
            controller.stop()
        return 0

    # imported here so headless mode never loads tkinter or pystray
    from .ui.tray import TrayApp

    controller, startup_warnings = build_controller(engine, injector, dictionaries, settings)
    tray = TrayApp(controller, dictionaries, store, start_hidden=args.minimized, hook_error=native.hook_error)
    controller.set_listener(tray)  # breaks the tray <-> controller cycle
    controller.start()
    deliver_warnings(
        tray.on_error, notices + startup_warnings + _start_function_keys(controller, store.directory, injector)
    )
    show_update_result(store.directory)
    try:
        tray.run()
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
