"""Entry point: wires everything together (the only place that builds concrete classes)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from ._native import load_native
from .controller import MorseController, NativeInjector
from .dictionary import DictionaryManager
from .settings import SettingsStore

log = logging.getLogger("mourse_decoder")


class _ConsoleListener:
    """Output for --headless."""

    def on_symbol(self, symbol: str) -> None:
        print(symbol, end="", flush=True)

    def on_letter(self, sequence: str, text: str | None) -> None:
        print(f"  → {text if text is not None else '?'}", flush=True)

    def on_error(self, message: str) -> None:
        print(f"Fehler: {message}", file=sys.stderr, flush=True)


class _PrintInjector:
    """For --dry-run: show instead of typing."""

    def inject_text(self, text: str) -> None:
        print(f"[tippe {text!r}]", flush=True)

    def inject_backspaces(self, count: int) -> None:
        print(f"[lösche {count}]", flush=True)


def _prepare_windows() -> None:
    """Windowed builds have no console (stdout/stderr are None), and the taskbar should show
    our icon instead of grouping the window under python.exe."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))  # noqa: SIM115 - lives as long as the process
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

    try:
        native = load_native()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    store = SettingsStore()
    settings = store.load()
    dictionaries = DictionaryManager(
        telecode_db=store.directory / "telecode.sqlite",
        user_directory=store.directory / "alphabets",
        user_smiley_directory=store.directory / "smileys",
    )
    engine = native.MorseEngine(trigger=settings.trigger)
    injector = _PrintInjector() if args.dry_run else NativeInjector(native)

    if args.headless:
        controller = MorseController(engine, injector, dictionaries, settings, listener=_ConsoleListener())
        controller.start()
        print(f"Morse-Decoder läuft (Taster: {settings.trigger}, Alphabet: {settings.alphabet}). Strg+C beendet.")
        try:
            while True:
                time.sleep(0.5)
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

    controller = MorseController(engine, injector, dictionaries, settings)
    tray = TrayApp(controller, dictionaries, store, start_hidden=args.minimized)
    controller.set_listener(tray)  # breaks the tray <-> controller cycle
    controller.start()
    try:
        tray.run()
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
