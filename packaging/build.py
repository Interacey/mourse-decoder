"""Build the Windows executable with PyInstaller.

Run from the project root after the native module is built (``maturin develop --release``):

    python packaging/build.py

Output: dist/MourseDecoder.exe (one file, no console window).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "python" / "mourse_decoder"
ICON = ROOT / "packaging" / "icon.ico"
NAME = "MourseDecoder"

# JSON data that is read at runtime, next to the package code
DATA_DIRS = ("alphabets", "smiley_sets", "locales")


def make_icon() -> None:
    """Write the app icon (the same mouse as in the UI) as a multi-size .ico."""
    sys.path.insert(0, str(ROOT / "python"))
    from mourse_decoder.ui.icon import create_icon

    create_icon(True, 256).save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main() -> int:
    if not list(PACKAGE.glob("_morse_core*.pyd")) and not list(PACKAGE.glob("_morse_core*.so")):
        print("Native module missing, run `maturin develop --release` first.", file=sys.stderr)
        return 1
    make_icon()
    separator = ";" if sys.platform == "win32" else ":"
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", NAME,
        "--icon", str(ICON),
        "--paths", str(ROOT / "python"),
        "--hidden-import", "mourse_decoder._morse_core",
        "--hidden-import", "pystray._win32",
        "--hidden-import", "PIL._tkinter_finder",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    for folder in DATA_DIRS:
        command += ["--add-data", f"{PACKAGE / folder}{separator}mourse_decoder/{folder}"]
    command.append(str(ROOT / "packaging" / "run.py"))
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
