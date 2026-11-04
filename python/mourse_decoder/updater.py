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
from .i18n import tr

REPO = "Interacey/mourse-decoder"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
GITHUB = "https://github.com/"  # every link we follow or open must start with this
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"  # opened when an update cannot run by itself
EXE_NAME = "MourseDecoder.exe"
TIMEOUT_S = 15
MAX_JSON_BYTES = 1 << 20  # the release description is a few KB
MAX_SHA_BYTES = 1024
MAX_EXE_BYTES = 200 << 20  # the exe is about 20 MB
TEMP_PREFIX = "mourse-update-"
# the only folder names uninstall deletes recursively, whatever APPDATA says
_OWN_FOLDERS = {"MourseDecoder", "mourse-decoder", "PolyglotMorse"}
RESULT_FILE = "update-result.txt"  # written by the helper script when it fails, shown and deleted at the next start
# PowerShell ends a single-quoted string at any of these, not only at the ASCII quote
_PS_QUOTES = "'\u2018\u2019\u201a\u201b"
_ALLOWED_HOSTS = {"github.com", "api.github.com", "objects.githubusercontent.com"}
_CREATE_NO_WINDOW = 0x08000000
_NEW_PROCESS_GROUP = 0x00000200  # DETACHED_PROCESS would leave PowerShell without a console, and it does nothing


class UpdateError(RuntimeError):
    """Something went wrong while looking for, loading or installing an update; the message is shown.

    ``open_page`` is True when downloading by hand from the releases page is a way out.
    """

    def __init__(self, message: str, open_page: bool = False) -> None:
        super().__init__(message)
        self.open_page = open_page


@dataclass(frozen=True)
class Release:
    tag: str
    notes: str
    exe_url: str
    sha_url: str | None
    page: str


def parse_version(text: str) -> tuple[int, ...]:
    """"v0.1.2" or "v.0.1.2" (the project's own style) -> (0, 1, 2); ValueError if the text has no version."""
    match = re.match(r"\s*v?\.?(\d+(?:\.\d+)*)", text, re.IGNORECASE)
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


def _network_error(exc: BaseException) -> UpdateError:
    """Turn what urllib raises into a message that says what to do."""
    import ssl
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):  # GitHub answers an exhausted anonymous quota (60 per hour and address) like this
            return UpdateError(tr("update.err.rate_limit"), open_page=True)
        return UpdateError(tr("update.err.http", code=exc.code), open_page=True)
    if isinstance(getattr(exc, "reason", exc), ssl.SSLError):
        return UpdateError(tr("update.err.ssl"), open_page=True)
    return UpdateError(tr("update.err.network", detail=exc), open_page=True)


def _allowed_url(url: str) -> bool:
    """https and a GitHub host (github.com, api.github.com, *.githubusercontent.com)."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme == "https" and (host in _ALLOWED_HOSTS or host.endswith(".githubusercontent.com"))


def _redirect_handler():
    import urllib.request

    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        """Every hop of a redirect chain must be https on a GitHub host, not only the last one."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not _allowed_url(newurl):
                raise UpdateError(f"Der Server hat auf eine unerlaubte Adresse umgeleitet: {newurl} – abgebrochen.")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return SafeRedirect()


def _open(url: str, urlopen: Callable | None, accept: str = "*/*"):
    """Open an https link; a redirect to plain http or another host is refused (urllib follows redirects itself)."""
    if not url.startswith("https://"):
        raise UpdateError(f"Unsicherer Download-Link: {url}")
    import urllib.request

    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "MourseDecoder"})
    import http.client

    try:
        opener = urlopen or urllib.request.build_opener(_redirect_handler()).open
        response = opener(request, timeout=TIMEOUT_S)
    except (OSError, http.client.HTTPException) as exc:  # URLError, HTTPError and timeouts are OSErrors
        raise _network_error(exc) from None
    final = getattr(response, "geturl", lambda: url)()
    if not final.startswith("https://") or (urlopen is None and not _allowed_url(final)):
        response.close()
        raise UpdateError("Der Server hat auf eine unverschlüsselte Adresse umgeleitet – abgebrochen.")
    return response


def fetch_latest(urlopen: Callable | None = None) -> Release:
    import http.client

    try:
        with _open(LATEST_URL, urlopen, "application/vnd.github+json") as response:
            raw = response.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise UpdateError("Die Antwort von GitHub ist viel zu groß.")
        data = json.loads(raw)
    except (OSError, http.client.HTTPException) as exc:  # the answer broke off while reading
        raise _network_error(exc) from None
    except ValueError:
        raise UpdateError(tr("update.err.network", detail="invalid response"), open_page=True) from None
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
        raise UpdateError(tr("update.err.no_exe", tag=tag, exe=EXE_NAME), open_page=True)
    return Release(tag, notes, assets[EXE_NAME], assets.get(f"{EXE_NAME}.sha256"), page)


def _github_only(url: str) -> str:
    if not url.startswith(GITHUB):
        raise UpdateError(f"Download-Link nicht von GitHub: {url}")
    return url


def download(release: Release, directory: Path, urlopen: Callable | None = None) -> Path:
    """Load the exe of ``release`` into ``directory`` and check it against the published SHA-256."""
    import http.client

    if release.sha_url is None:  # the check stays mandatory; the user can still download and judge by hand
        raise UpdateError(tr("update.err.no_checksum", tag=release.tag), open_page=True)
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
    except (OSError, http.client.HTTPException) as exc:  # includes a download that breaks off half-way
        target.unlink(missing_ok=True)
        raise UpdateError(f"Download fehlgeschlagen ({exc}).", open_page=True) from None
    except UpdateError:
        target.unlink(missing_ok=True)
        raise
    return target


def _quote(path: object) -> str:
    text = "".join(c * 2 if c in _PS_QUOTES else c for c in str(path))
    return "'" + text + "'"


def result_path(config_dir: Path | None = None) -> Path:
    if config_dir is None:
        from .settings import default_config_dir

        config_dir = default_config_dir()
    return Path(config_dir) / RESULT_FILE


# the helper script cannot show anything, so it leaves a code here; tr() turns it into a sentence
_RESULT_CODES = ("busy", "start", "uninstall", "reparse")


def read_update_result(config_dir: Path | None = None) -> str | None:
    """Text for a failed update/uninstall of the last run, or None. Reading deletes the file."""
    path = result_path(config_dir)
    try:
        code = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError):
        return None
    path.unlink(missing_ok=True)
    return tr(f"update.result.{code}" if code in _RESULT_CODES else "update.result.unknown")


def _status_function(status: Path | None) -> list[str]:
    """PowerShell: ``Report 'code'`` writes the one-line result file (no BOM) and never fails the script."""
    if status is None:
        return ["function Report($code) { }"]
    return [
        f"$statusFile = {_quote(status)}",
        "function Report($code) {",
        "  try {",
        "    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $statusFile) -ErrorAction Stop | Out-Null",
        "    [System.IO.File]::WriteAllText($statusFile, $code, (New-Object System.Text.UTF8Encoding $false))",
        "  } catch { }",
        "}",
    ]


def update_script(
    new_exe: Path, target: Path, start: bool = True, delay_s: int = 1, tries: int = 120, status: Path | None = None
) -> str:
    """Wait until ``target`` is free, put ``new_exe`` in its place, start it.

    Exit code 0 = done, 1 = ``target`` stayed busy and nothing changed, 2 = replaced but did not start.
    On exit 1 the old program is started again (if ``start``), the download folder is removed and
    ``status`` gets a one-line code for the next start to show.
    """
    download_folder = new_exe.parent
    # only the folder our own mkdtemp made; never one somebody else owns
    own_folder = download_folder.name.startswith(TEMP_PREFIX)
    remove = f"Remove-Item -LiteralPath {_quote(download_folder)} -Recurse -Force -ErrorAction Ignore"
    cleanup = [remove] if own_folder else []
    return "\n".join(
        [
            f"Start-Sleep -Seconds {delay_s}",
            *_status_function(status),
            f"$new = {_quote(new_exe)}; $target = {_quote(target)}; $done = $false",
            f"for ($i = 0; $i -lt {tries} -and -not $done; $i++) {{",
            # the .NET calls take paths literally; Move-Item -Destination would read [ ] as a wildcard.
            # Delete fails while the exe runs; if Move fails after Delete, the next round only repeats Move
            "  try { [System.IO.File]::Delete($target); [System.IO.File]::Move($new, $target); $done = $true }",
            "  catch { Start-Sleep -Milliseconds 500 }",
            "}",
            "if (-not $done) {",
            "  Report 'busy'",
            *[f"  {line}" for line in cleanup],
            *(["  try { Start-Process -FilePath $target -ErrorAction Stop } catch { }"] if start else []),
            "  exit 1",
            "}",
            *cleanup,
            *(["Remove-Item -LiteralPath $statusFile -Force -ErrorAction Ignore"] if status else []),
            *(["try { Start-Process -FilePath $target -ErrorAction Stop } catch { Report 'start'; exit 2 }"] * start),
            "exit 0",
        ]
    )


def uninstall_script(
    exe: Path | None, folders: list[Path], delay_s: int = 1, tries: int = 120, status: Path | None = None
) -> str:
    """Wait until ``exe`` can be deleted, then delete it and the ``folders``.

    A folder that is a junction or symlink is skipped, not followed.
    Exit code 0 = everything is gone, 1 = something is still there (``status`` gets a code).
    """
    lines = [f"Start-Sleep -Seconds {delay_s}", *_status_function(status), "$code = ''"]
    if exe is not None:
        lines += [
            f"$exe = {_quote(exe)}",
            f"for ($i = 0; $i -lt {tries} -and (Test-Path -LiteralPath $exe); $i++) {{",
            "  Remove-Item -LiteralPath $exe -Force -ErrorAction Ignore",
            "  if (Test-Path -LiteralPath $exe) { Start-Sleep -Milliseconds 500 }",
            "}",
        ]
    for folder in folders:
        lines += [
            f"$f = Get-Item -LiteralPath {_quote(folder)} -Force -ErrorAction Ignore",
            "if ($f -and ($f.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { $code = 'reparse' }",
            "elseif ($f) { Remove-Item -LiteralPath $f.FullName -Recurse -Force -ErrorAction Ignore }",
        ]
    paths = ", ".join(_quote(p) for p in [*([exe] if exe is not None else []), *folders])
    lines += [
        f"if (@({paths}) | Where-Object {{ Test-Path -LiteralPath $_ }}) {{",
        "  if (-not $code) { $code = 'uninstall' }",
        "  Report $code",
        "  exit 1",
        "}",
        "exit 0",
    ]
    return "\n".join(lines)


def _powershell() -> str:
    """The Windows PowerShell by full path, so a changed PATH can't hide or replace it."""
    root = os.environ.get("SystemRoot")
    if root:
        path = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if path.is_file():
            return str(path)
    return "powershell.exe"


def _can_write(folder: Path) -> bool:
    """Try to create a file there: os.access() looks only at the read-only flag on Windows, not at the permissions."""
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def run_after_exit(script: str, popen: Callable | None = None) -> None:
    """Start ``script`` in a hidden, detached PowerShell that outlives this process."""
    import subprocess

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # a one-file exe passes its unpack folder on to children; a new exe started from here must unpack its own
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    # no -ExecutionPolicy: it only matters for script files, and the encoded command is none
    try:
        (popen or subprocess.Popen)(
            [_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            creationflags=_CREATE_NO_WINDOW | _NEW_PROCESS_GROUP,
            close_fds=True,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:  # not found, or blocked by a policy
        raise UpdateError(tr("update.err.no_powershell", detail=exc), open_page=True) from None


def install(release: Release, urlopen: Callable | None = None, popen: Callable | None = None) -> None:
    """Download ``release`` and schedule the swap. The caller must quit the app right afterwards."""
    if not can_self_update():
        raise UpdateError("Automatisch aktualisieren geht nur mit der heruntergeladenen MourseDecoder.exe.")
    target = Path(sys.executable)
    if not _can_write(target.parent):
        raise UpdateError(tr("update.err.no_write", folder=target.parent), open_page=True)
    folder = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        new_exe = download(release, folder, urlopen)
        run_after_exit(update_script(new_exe, target, status=result_path()), popen)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)  # nothing downloaded is worth keeping
        raise


def uninstall_targets(config_dir: Path, delete_data: bool = True) -> list[Path]:
    """Everything the program leaves on the disk besides the exe: settings, own files, the old folder.

    With ``delete_data`` False the list is empty, but the name check still runs.

    Only folders with the program's own names are ever returned, so a wrong APPDATA can't point
    the recursive delete at something else.
    """
    config_dir = Path(config_dir)
    if config_dir.name not in _OWN_FOLDERS:
        raise UpdateError(f"Ordner '{config_dir}' gehört nicht zum Programm – nichts gelöscht.")
    if not delete_data:
        return []
    legacy = config_dir.with_name(config_dir.name.replace("MourseDecoder", "PolyglotMorse"))
    return [config_dir, *([legacy] if legacy != config_dir else [])]


def uninstall(config_dir: Path, popen: Callable | None = None, delete_data: bool = True) -> None:
    """Remove the program (from the exe) and, if ``delete_data``, settings, cache and own files.

    The files are removed after this app has ended, so the caller must quit right afterwards.
    """
    folders = uninstall_targets(config_dir, delete_data)
    exe = Path(sys.executable) if can_self_update() else None
    if exe is not None and not _can_write(exe.parent):  # the script could not delete it, and nobody would see that
        raise UpdateError(tr("update.err.no_write", folder=exe.parent))
    if sys.platform != "win32":
        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)
        return
    if exe is not None or folders:
        status = None if delete_data else result_path(config_dir)  # a deleted folder must not get the file back
        run_after_exit(uninstall_script(exe, folders, status=status), popen)
