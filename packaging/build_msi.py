"""Build the per-user MSI installer with WiX 5.

Run from the project root after packaging/build.py has produced the exe:

    dotnet tool install --global wix --version 5.0.2
    python packaging/build_msi.py

Output: dist/MourseDecoder.msi (installs to %LOCALAPPDATA%\\Programs\\MourseDecoder). The version comes from
python/mourse_decoder/__init__.py unless --version is given. WiX is pinned to 5.0.2: later major versions
ask for an EULA acceptance.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WXS = ROOT / "packaging" / "wix" / "MourseDecoder.wxs"
ICON = ROOT / "packaging" / "icon.ico"
INIT = ROOT / "python" / "mourse_decoder" / "__init__.py"
WIX_INSTALL = "dotnet tool install --global wix --version 5.0.2"


def msi_version(text: str) -> str:
    """`0.1.10`, `v.0.1.10` or `v0.1.10` as an MSI ProductVersion. Windows Installer reads only three fields,
    with limits 255, 255 and 65535, and ignores a fourth."""
    match = re.fullmatch(r"(?:v\.?)?([0-9]+)\.([0-9]+)\.([0-9]+)", text, re.ASCII)
    if not match:
        raise ValueError(f"Bad version {text!r}: expected three numeric fields like 0.1.10")
    major, minor, build = (int(part) for part in match.groups())
    if major > 255 or minor > 255 or build > 65535:
        raise ValueError(f"Version {text!r} is too large for an MSI: limits are 255.255.65535")
    return f"{major}.{minor}.{build}"


def package_version() -> str:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"), re.M)
    if not match:
        raise ValueError(f"No __version__ found in {INIT}")
    return match[1]


def wix_command(wix: str, exe: Path, version: str, output: Path) -> list[str]:
    return [
        wix, "build", str(WXS),
        "-arch", "x64",
        "-d", f"Version={version}",
        "-d", f"ExePath={exe}",
        "-d", f"IconPath={ICON}",
        "-o", str(output),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the MourseDecoder MSI with WiX.")
    parser.add_argument("--exe", type=Path, default=ROOT / "dist" / "MourseDecoder.exe", help="the exe to package")
    parser.add_argument("--version", help="MSI version, e.g. 0.1.10 (default: __version__ of the package)")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "MourseDecoder.msi")
    args = parser.parse_args(argv)
    try:
        version = msi_version(args.version or package_version())
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    wix = shutil.which("wix")
    if wix is None:
        print(f"wix is not on PATH. Install it with `{WIX_INSTALL}`.", file=sys.stderr)
        return 1
    exe = args.exe.resolve()
    if not exe.is_file():
        print(f"{exe} not found, run packaging/build.py first.", file=sys.stderr)
        return 1
    if not ICON.is_file():
        print(f"{ICON} not found, run packaging/build.py first (it writes the icon).", file=sys.stderr)
        return 1
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    code = subprocess.call(wix_command(wix, exe, version, output), cwd=ROOT)
    if code != 0:
        return code
    # `wix build` does not run the ICE checks; this fails on errors such as a per-user folder that is not removed
    return subprocess.call([wix, "msi", "validate", str(output)], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
