"""Update to the latest GitHub release, and remove the program completely (no tkinter).

A running exe can neither overwrite nor delete itself. So both jobs hand a small PowerShell
script to a hidden process, which waits until this app has ended and then does the work.

Network and process modules are imported only when they are needed: together they cost about
5 MB, and most runs never open the update page.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

from . import __version__

REPO = "Interacey/mourse-decoder"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
GITHUB = "https://github.com/"  # every link we follow or open must start with this
EXE_NAME = "MourseDecoder.exe"
TIMEOUT_S = 15
MAX_JSON_BYTES = 1 << 20  # the release description is a few KB
MAX_SHA_BYTES = 1024
MAX_EXE_BYTES = 200 << 20  # the exe is about 20 MB
TEMP_PREFIX = "mourse-update-"
# the only folder names uninstall deletes recursively, whatever APPDATA says
_OWN_FOLDERS = {"MourseDecoder", "mourse-decoder", "PolyglotMorse"}
_CREATE_NO_WINDOW = 0x08000000
_NEW_PROCESS_GROUP = 0x00000200  # DETACHED_PROCESS would leave PowerShell without a console, and it does nothing


class UpdateError(RuntimeError):
    """Something went wrong while looking for, loading or installing an update; the message is shown."""


@dataclass(frozen=True)
class Release:
    tag: str
    notes: str
    exe_url: str
    sha_url: str | None
    page: str


def parse_version(text: str) -> tuple[int, ...]:
    """"v0.1.2" or "v.0.1.2" (the project's own style) -> (0, 1, 2); ValueError if the text has no version."""
    match = re.match(r"\s*v?\.?(\d+(?:\.\d+)*)", text)
    if match is None:
        raise ValueError(f"keine Version: {text!r}")
    return tuple(int(part) for part in match[1].split("."))


def is_newer(tag: str, current: str = __version__) -> bool:
    """True if ``tag`` is a higher version than ``current`` ("1.0" equals "1.0.0")."""
    pairs = zip_longest(parse_version(tag), parse_version(current), fillvalue=0)
    return next(((a > b) for a, b in pairs if a != b), False)


def can_self_update() -> bool:
    """Only the downloaded exe can replace itself; a source checkout updates through git."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _open(url: str, urlopen: Callable | None, accept: str = "*/*"):
    """Open an https link; a redirect to plain http is refused (urllib follows redirects by itself)."""
    if not url.startswith("https://"):
        raise UpdateError(f"Unsicherer Download-Link: {url}")
    import urllib.request

    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "MourseDecoder"})
    try:
        response = (urlopen or urllib.request.urlopen)(request, timeout=TIMEOUT_S)
    except OSError as exc:  # URLError and HTTPError are OSErrors
        raise UpdateError(f"Keine Verbindung ({exc}).") from None
    final = getattr(response, "geturl", lambda: url)()
    if not final.startswith("https://"):
        response.close()
        raise UpdateError("Der Server hat auf eine unverschlüsselte Adresse umgeleitet – abgebrochen.")
    return response


def fetch_latest(urlopen: Callable | None = None) -> Release:
    try:
        with _open(LATEST_URL, urlopen, "application/vnd.github+json") as response:
            raw = response.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise UpdateError("Die Antwort von GitHub ist viel zu groß.")
        data = json.loads(raw)
    except OSError as exc:
        raise UpdateError(f"Keine Verbindung zu GitHub ({exc}).") from None
    except ValueError:
        raise UpdateError("Keine Verbindung zu GitHub (unlesbare Antwort).") from None
    try:
        assets = {a["name"]: a["browser_download_url"] for a in data["assets"]}
        tag, page = str(data["tag_name"]), str(data["html_url"])
        notes = str(data.get("body") or "").strip()
        parse_version(tag)
        if not page.startswith(GITHUB):
            raise ValueError(page)
    except (KeyError, TypeError, ValueError):
        raise UpdateError("Unerwartete Antwort von GitHub.") from None
    if EXE_NAME not in assets:
        raise UpdateError(f"Das Release {tag} enthält keine {EXE_NAME}.")
    return Release(tag, notes, assets[EXE_NAME], assets.get(f"{EXE_NAME}.sha256"), page)


def _github_only(url: str) -> str:
    if not url.startswith(GITHUB):
        raise UpdateError(f"Download-Link nicht von GitHub: {url}")
    return url


def download(release: Release, directory: Path, urlopen: Callable | None = None) -> Path:
    """Load the exe of ``release`` into ``directory`` and check it against the published SHA-256."""
    if release.sha_url is None:
        raise UpdateError(f"Das Release {release.tag} hat keine Prüfsumme – Update abgebrochen.")
    with _open(_github_only(release.sha_url), urlopen) as response:
        words = response.read(MAX_SHA_BYTES).decode("ascii", "replace").split()
    expected = words[0].lower() if words else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("Die Prüfsumme des Releases ist ungültig.")

    target = Path(directory) / EXE_NAME
    digest = hashlib.sha256()
    size, head = 0, b""
    try:
        with _open(_github_only(release.exe_url), urlopen) as response, target.open("wb") as out:
            while chunk := response.read(1 << 16):
                size += len(chunk)
                if size > MAX_EXE_BYTES:
                    raise UpdateError("Die Datei ist viel größer als erwartet – Update abgebrochen.")
                head = head or chunk[:2]
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected:
            raise UpdateError("Die geladene Datei passt nicht zur Prüfsumme – Update abgebrochen.")
        if head != b"MZ":
            raise UpdateError("Die geladene Datei ist kein Windows-Programm – Update abgebrochen.")
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise UpdateError(f"Download fehlgeschlagen ({exc}).") from None
    except UpdateError:
        target.unlink(missing_ok=True)
        raise
    return target


def _quote(path: object) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def update_script(new_exe: Path, target: Path, start: bool = True, delay_s: int = 1, tries: int = 120) -> str:
    """Wait until ``target`` is free, put ``new_exe`` in its place, start it.

    Exit code 0 = done, 1 = ``target`` stayed busy and nothing changed, 2 = replaced but did not start.
    """
    download_folder = new_exe.parent
    return "\n".join(
        [
            f"Start-Sleep -Seconds {delay_s}",
            f"$new = {_quote(new_exe)}; $target = {_quote(target)}; $done = $false",
            f"for ($i = 0; $i -lt {tries} -and -not $done; $i++) {{",
            "  try { Move-Item -LiteralPath $new -Destination $target -Force -ErrorAction Stop; $done = $true }",
            "  catch { Start-Sleep -Milliseconds 500 }",
            "}",
            "if (-not $done) { exit 1 }",
            # the folder our own mkdtemp made; never a folder somebody else owns
            *(
                [f"Remove-Item -LiteralPath {_quote(download_folder)} -Recurse -Force -ErrorAction Ignore"]
                if download_folder.name.startswith(TEMP_PREFIX)
                else []
            ),
            *(["try { Start-Process -FilePath $target -ErrorAction Stop } catch { exit 2 }"] if start else []),
            "exit 0",
        ]
    )


def uninstall_script(exe: Path | None, folders: list[Path], delay_s: int = 1, tries: int = 120) -> str:
    """Wait until ``exe`` can be deleted, then delete it and the ``folders``.

    Exit code 0 = everything is gone, 1 = something is still there.
    """
    lines = [f"Start-Sleep -Seconds {delay_s}"]
    if exe is not None:
        lines += [
            f"$exe = {_quote(exe)}",
            f"for ($i = 0; $i -lt {tries} -and (Test-Path -LiteralPath $exe); $i++) {{",
            "  Remove-Item -LiteralPath $exe -Force -ErrorAction Ignore",
            "  if (Test-Path -LiteralPath $exe) { Start-Sleep -Milliseconds 500 }",
            "}",
        ]
    lines += [f"Remove-Item -LiteralPath {_quote(f)} -Recurse -Force -ErrorAction Ignore" for f in folders]
    paths = ", ".join(_quote(p) for p in [*([exe] if exe is not None else []), *folders])
    lines += [f"if (@({paths}) | Where-Object {{ Test-Path -LiteralPath $_ }}) {{ exit 1 }}", "exit 0"]
    return "\n".join(lines)


def run_after_exit(script: str, popen: Callable | None = None) -> None:
    """Start ``script`` in a hidden, detached PowerShell that outlives this process."""
    import subprocess

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # a one-file exe passes its unpack folder on to children; a new exe started from here must unpack its own
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    # no -ExecutionPolicy: it only matters for script files, and the encoded command is none
    (popen or subprocess.Popen)(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        creationflags=_CREATE_NO_WINDOW | _NEW_PROCESS_GROUP,
        close_fds=True,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def install(release: Release, urlopen: Callable | None = None, popen: Callable | None = None) -> None:
    """Download ``release`` and schedule the swap. The caller must quit the app right afterwards."""
    if not can_self_update():
        raise UpdateError("Automatisch aktualisieren geht nur mit der heruntergeladenen MourseDecoder.exe.")
    target = Path(sys.executable)
    if not os.access(target.parent, os.W_OK):
        raise UpdateError(f"Kein Schreibrecht im Ordner {target.parent}.")
    folder = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        new_exe = download(release, folder, urlopen)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)  # nothing downloaded is worth keeping
        raise
    run_after_exit(update_script(new_exe, target), popen)


def uninstall_targets(config_dir: Path) -> list[Path]:
    """Everything the program leaves on the disk besides the exe: settings, own files, the old folder.

    Only folders with the program's own names are ever returned, so a wrong APPDATA can't point
    the recursive delete at something else.
    """
    config_dir = Path(config_dir)
    if config_dir.name not in _OWN_FOLDERS:
        raise UpdateError(f"Ordner '{config_dir}' gehört nicht zum Programm – nichts gelöscht.")
    legacy = config_dir.with_name(config_dir.name.replace("MourseDecoder", "PolyglotMorse"))
    return [config_dir, *([legacy] if legacy != config_dir else [])]


def uninstall(config_dir: Path, popen: Callable | None = None) -> None:
    """Remove settings, own alphabets/smileys/languages and (from the exe) the program itself.

    The files are removed after this app has ended, so the caller must quit right afterwards.
    """
    folders = uninstall_targets(config_dir)
    if sys.platform != "win32":
        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)
        return
    run_after_exit(uninstall_script(Path(sys.executable) if can_self_update() else None, folders), popen)
