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
MSI_NAME = "MourseDecoder.msi"
REG_KEY = r"Software\Interacey\MourseDecoder"  # written by the installer: InstallDir and ProductCode
MSI_LOG_FILE = "msi-update.log"  # msiexec's log of an update, next to the settings
TIMEOUT_S = 15
MAX_JSON_BYTES = 1 << 20  # the release description is a few KB
MAX_SHA_BYTES = 1024
MAX_SIG_BYTES = 1024  # a base64 signature is 89 bytes
MAX_EXE_BYTES = 200 << 20  # the exe is about 20 MB
MAX_MSI_BYTES = 200 << 20  # the installer holds the same exe in a cabinet, so it is about as large
MSI_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")  # OLE compound file, the container of every .msi
# a ProductCode goes into a script, so only this shape is accepted
_GUID = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")
TEMP_PREFIX = "mourse-update-"
# the only folder names uninstall deletes recursively, whatever APPDATA says
_OWN_FOLDERS = {"MourseDecoder", "mourse-decoder", "PolyglotMorse"}
RESULT_FILE = "update-result.txt"  # written by the helper script when it fails, shown and deleted at the next start
BACKUP_SUFFIX = ".old"  # the replaced exe waits under this name next to the new one until the swap is done
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
    page: str  # html_url from the release JSON; never opened, the update page opens RELEASES_URL
    sig_url: str | None = None  # MourseDecoder.exe.sig; an update is never installed without it
    # the installer, its checksum and its signature: all three or none; an MSI-installed copy updates only with them
    msi_url: str | None = None
    msi_sha_url: str | None = None
    msi_sig_url: str | None = None


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


def _registry_string(name: str) -> str | None:
    """A string value of HKCU\\Software\\Interacey\\MourseDecoder, None if it is missing or the registry fails."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except (OSError, ImportError):  # no key, no access, not Windows
        return None
    return value if isinstance(value, str) else None


def install_kind() -> str:
    """"msi" if the Windows Installer put this exe where it runs, "portable" for any other exe, "source" otherwise.

    The installer records its folder in the registry; the exe is the installed one only if it runs from there.
    """
    if not getattr(sys, "frozen", False):
        return "source"
    if sys.platform == "win32":
        import ntpath

        folder = _registry_string("InstallDir")
        # ntpath on purpose: these are Windows paths, whatever the platform of the test run
        running_from = ntpath.dirname(ntpath.normpath(sys.executable))
        if folder and ntpath.normcase(ntpath.normpath(folder)) == ntpath.normcase(running_from):
            return "msi"
    return "portable"


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
        raise UpdateError(tr("update.err.no_exe", tag=tag, exe=EXE_NAME))  # nothing to install and nothing to offer
    msi = [assets.get(name) for name in (MSI_NAME, f"{MSI_NAME}.sha256", f"{MSI_NAME}.sig")]
    if None in msi:  # a partial set counts as no installer: install() refuses it, it is never half-trusted
        msi = [None, None, None]
    exe_sha, exe_sig = assets.get(f"{EXE_NAME}.sha256"), assets.get(f"{EXE_NAME}.sig")
    return Release(tag, notes, assets[EXE_NAME], exe_sha, page, exe_sig, *msi)


def _github_only(url: str) -> str:
    if not url.startswith(GITHUB):
        raise UpdateError(f"Download-Link nicht von GitHub: {url}")
    return url


def _small_file(url: str, limit: int, urlopen: Callable | None) -> bytes:
    """A small release file (checksum, signature) of at most ``limit`` bytes; a longer answer is an error."""
    import http.client

    try:
        with _open(_github_only(url), urlopen) as response:
            data = response.read(limit + 1)
    except (OSError, http.client.HTTPException) as exc:  # the answer broke off while reading
        raise _network_error(exc) from None
    if len(data) > limit:
        raise UpdateError(tr("update.err.file_too_large", name=url.rsplit("/", 1)[-1]))
    return data


def _require_newer(tag: str) -> None:
    """Refuse a release that is not newer than this program, however it was signed (no downgrade, no reinstall)."""
    try:
        newer = is_newer(tag)
    except ValueError:
        newer = False
    if not newer:
        raise UpdateError(tr("update.err.not_newer", tag=tag, current=__version__))


def download(release: Release, directory: Path, urlopen: Callable | None = None) -> Path:
    """Load the exe of ``release`` into ``directory``; it must match the published SHA-256 and carry a valid signature.

    The signature is checked against the hash computed here, not the published one, so a changed
    release can only pass with a signature made by a pinned key. Any failure deletes the download.
    """
    return _download(release, directory, urlopen)[0]


def _download(release: Release, directory: Path, urlopen: Callable | None) -> tuple[Path, str]:
    """``download`` plus the SHA-256 (lower-case hex) that was checked and signed."""
    _require_newer(release.tag)
    # No download page for a release that lacks a checksum or signature: whoever can edit a release can swap the exe
    # and delete the .sig, and a manual download would then run the swapped exe with no check at all.
    if release.sha_url is None:
        raise UpdateError(tr("update.err.no_checksum", tag=release.tag))
    if release.sig_url is None:
        raise UpdateError(tr("update.err.no_signature", tag=release.tag))
    return _fetch_verified(
        release.tag, directory, urlopen, EXE_NAME, release.exe_url, release.sha_url, release.sig_url,
        b"MZ", MAX_EXE_BYTES, "Die geladene Datei ist kein Windows-Programm – Update abgebrochen.",
    )


def _download_msi(release: Release, directory: Path, urlopen: Callable | None) -> tuple[Path, str]:
    """Like ``_download`` for the installer; a release without all three installer files is refused (``no_msi``)."""
    _require_newer(release.tag)
    if release.msi_url is None or release.msi_sha_url is None or release.msi_sig_url is None:
        raise UpdateError(tr("update.err.no_msi", tag=release.tag), open_page=True)
    return _fetch_verified(
        release.tag, directory, urlopen, MSI_NAME, release.msi_url, release.msi_sha_url, release.msi_sig_url,
        MSI_MAGIC, MAX_MSI_BYTES, "Die geladene Datei ist kein Windows-Installer-Paket – Update abgebrochen.",
    )


def _fetch_verified(
    tag: str, directory: Path, urlopen: Callable | None, name: str, url: str, sha_url: str, sig_url: str,
    magic: bytes, limit: int, wrong_type: str,
) -> tuple[Path, str]:
    """Load ``name`` into ``directory``: it must be at most ``limit`` bytes, start with ``magic``, match the
    checksum and carry a signature of a pinned key for ``tag``, ``name`` and its own hash. Any failure deletes it."""
    import http.client

    from . import signing

    if not signing.PINNED_PUBLIC_KEYS:  # verify_release says the same, but only after 20 MB were loaded
        # open_page stays True here: this is a property of the installed build, not of the release, so nobody
        # who edits a release can cause it, and a build without a key has no other way to get an update
        raise signing.SignatureError("no_pinned_key")
    words = _small_file(sha_url, MAX_SHA_BYTES, urlopen).decode("ascii", "replace").split()
    expected = words[0].lower() if words else ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("Die Prüfsumme des Releases ist ungültig.")
    signature = _small_file(sig_url, MAX_SIG_BYTES, urlopen)

    target = Path(directory) / name
    digest = hashlib.sha256()
    size, head = 0, b""
    try:
        with _open(_github_only(url), urlopen) as response, target.open("wb") as out:
            while chunk := response.read(1 << 16):
                size += len(chunk)
                if size > limit:
                    raise UpdateError("Die Datei ist viel größer als erwartet – Update abgebrochen.")
                head += chunk[: len(magic) - len(head)]
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected:
            raise UpdateError("Die geladene Datei passt nicht zur Prüfsumme – Update abgebrochen.")
        if head != magic:
            raise UpdateError(wrong_type)
        signing.verify_release(tag, name, digest.hexdigest(), signature)
    except (OSError, http.client.HTTPException) as exc:  # includes a download that breaks off half-way
        target.unlink(missing_ok=True)
        raise UpdateError(f"Download fehlgeschlagen ({exc}).", open_page=True) from None
    except UpdateError:
        target.unlink(missing_ok=True)
        raise
    return target, digest.hexdigest()


def _quote(path: object) -> str:
    text = "".join(c * 2 if c in _PS_QUOTES else c for c in str(path))
    return "'" + text + "'"


def _backup_path(exe: Path) -> Path:
    return exe.with_name(exe.name + BACKUP_SUFFIX)


def result_path(config_dir: Path | None = None) -> Path:
    if config_dir is None:
        from .settings import default_config_dir

        config_dir = default_config_dir()
    return Path(config_dir) / RESULT_FILE


# the helper script cannot show anything, so it leaves a code here; tr() turns it into a sentence
_RESULT_CODES = ("busy", "start", "uninstall", "reparse", "verify", "rollback", "msi")


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
    new_exe: Path,
    target: Path,
    start: bool = True,
    delay_s: int = 1,
    tries: int = 120,
    status: Path | None = None,
    sha256: str | None = None,
) -> str:
    """Wait until ``target`` is free, put ``new_exe`` in its place, start it.

    Exit code 0 = done, 1 = ``target`` stayed busy and nothing changed, 2 = replaced but did not start,
    3 = ``new_exe`` no longer has the hash ``sha256`` (64 hex digits) and nothing was replaced,
    4 = a failed swap could not be undone (the new file could not be deleted from ``target``, or the backup could
    not be renamed back): nothing is started, because ``target`` may hold a file that was not verified.
    The old exe is never deleted before the new one is in place: it is renamed to ``<target>.old`` and renamed back
    when anything fails after that, so a failed swap leaves the old exe, never a folder without one.
    The hash is checked before the rename, again before the move and once more on the file that was moved in,
    because the download sat in the temp folder since it was verified. Without ``sha256`` nothing is hashed.
    On exit 1 and 3 the old program is started again (if ``start``), the download folder is removed and
    ``status`` gets a one-line code for the next start to show. On exit 4 ``status`` gets a code too, but the
    download folder stays and no round is retried, since another round would overwrite the backup.
    """
    if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise ValueError("sha256 must be 64 hex digits")
    download_folder = new_exe.parent
    # only the folder our own mkdtemp made; never one somebody else owns
    own_folder = download_folder.name.startswith(TEMP_PREFIX)
    remove = f"Remove-Item -LiteralPath {_quote(download_folder)} -Recurse -Force -ErrorAction Ignore"
    cleanup = [remove] if own_folder else []
    restart = ["  try { Start-Process -FilePath $target -ErrorAction Stop } catch { }"] if start else []

    def differs(var: str) -> str:
        if sha256 is None:
            return "$false"
        return f"(Get-FileHash -LiteralPath {var} -Algorithm SHA256).Hash -ne '{sha256.lower()}'"

    # the .NET calls take paths literally; Move-Item -Destination would read [ ] as a wildcard.
    # A running exe can be renamed but not opened for writing, so the exclusive Open is what waits for the app to end.
    # $renamed / $placed say what a failed round has to undo; the undo is retried, a virus scanner may hold a file
    attempt = [
        "  try {",
        f"    if ({differs('$new')}) {{ $bad = $true }}",
        "    else {",
        "      if (Test-Path -LiteralPath $target) {",
        "        [System.IO.File]::Open($target, 'Open', 'ReadWrite', 'None').Close()",
        "        [System.IO.File]::Delete($old); [System.IO.File]::Move($target, $old); $renamed = $true",
        "      }",
        f"      if ({differs('$new')}) {{ $bad = $true }}",
        "      else {",
        "        $placed = $true; [System.IO.File]::Move($new, $target)",
        f"        if ({differs('$target')}) {{ $bad = $true }} else {{ $done = $true }}",
        "      }",
        "    }",
        "  } catch { }",
        "  if (-not $done) {",
        "    for ($r = 0; $r -lt 40 -and ($placed -or $renamed); $r++) {",
        "      try {",
        "        if ($placed) { [System.IO.File]::Delete($target); $placed = $false }",
        "        if ($renamed) { [System.IO.File]::Move($old, $target); $renamed = $false }",
        "      } catch { Start-Sleep -Milliseconds 500 }",
        "    }",
        "    if (-not $bad) { Start-Sleep -Milliseconds 500 }",
        "  }",
    ]
    return "\n".join(
        [
            f"Start-Sleep -Seconds {delay_s}",
            *_status_function(status),
            f"$new = {_quote(new_exe)}; $target = {_quote(target)}; $old = {_quote(_backup_path(target))}",
            "$done = $false; $bad = $false; $renamed = $false; $placed = $false",
            # a round whose undo failed ends the loop: the next round would delete the backup of the old exe
            f"for ($i = 0; $i -lt {tries} -and -not $done -and -not $bad -and -not ($placed -or $renamed); $i++) {{",
            *attempt,
            "}",
            # $target may hold the file that failed its check, so nothing may be started from here
            "if (-not $done -and ($placed -or $renamed)) {",
            "  Report 'rollback'",
            "  exit 4",
            "}",
            *(
                [
                    "if ($bad) {",
                    "  Report 'verify'",
                    *[f"  {line}" for line in cleanup],
                    *restart,
                    "  exit 3",
                    "}",
                ]
                if sha256 is not None
                else []
            ),
            "if (-not $done) {",
            "  Report 'busy'",
            *[f"  {line}" for line in cleanup],
            *restart,
            "  exit 1",
            "}",
            "try { [System.IO.File]::Delete($old) } catch { }",
            *cleanup,
            *(["Remove-Item -LiteralPath $statusFile -Force -ErrorAction Ignore"] if status else []),
            *(["try { Start-Process -FilePath $target -ErrorAction Stop } catch { Report 'start'; exit 2 }"] * start),
            "exit 0",
        ]
    )


def _wait_for_exe(tries: int) -> list[str]:
    """PowerShell: set ``$free`` once ``$target`` can be opened for writing, which a running exe does not allow."""
    return [
        "$free = $false",
        f"for ($i = 0; $i -lt {tries} -and -not $free; $i++) {{",
        "  try {",
        "    if (Test-Path -LiteralPath $target) {",
        "      [System.IO.File]::Open($target, 'Open', 'ReadWrite', 'None').Close()",
        "    }",
        "    $free = $true",
        "  } catch { Start-Sleep -Milliseconds 500 }",
        "}",
    ]


def _msiexec(msiexec: Path | None) -> str:
    """PowerShell expression for the Windows Installer; ``msiexec`` only replaces it in tests."""
    return '"$env:SystemRoot\\System32\\msiexec.exe"' if msiexec is None else _quote(msiexec)


def msi_update_script(
    msi: Path,
    target: Path,
    status: Path | None,
    sha256: str,
    log: Path,
    start: bool = True,
    delay_s: int = 1,
    tries: int = 120,
    msiexec: Path | None = None,
) -> str:
    """Wait until ``target`` is free, check ``msi`` against ``sha256``, install it with msiexec, start ``target``.

    The installer replaces the program inside its own folder, so the exe is never swapped by hand there. The
    arguments of msiexec are fixed text around two local paths; nothing from the release reaches the command line.
    Exit code 0 = installed, 1 = ``target`` stayed busy, 2 = installed but did not start, 3 = ``msi`` no longer has
    the hash ``sha256`` (64 hex digits) and msiexec did not run, 5 = msiexec failed. msiexec's own 0 and 3010
    (restart wanted; not needed for a per-user install) count as installed.
    On 1 and 3 the download folder is removed; on 5 it stays, next to ``log`` with msiexec's details. On every
    failure ``status`` gets a one-line code for the next start and the old program is started again if it is still
    there (if ``start``).
    """
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise ValueError("sha256 must be 64 hex digits")
    download_folder = msi.parent
    own_folder = download_folder.name.startswith(TEMP_PREFIX)  # only the folder our own mkdtemp made
    cleanup = [f"Remove-Item -LiteralPath {_quote(download_folder)} -Recurse -Force -ErrorAction Ignore"] * own_folder
    restart = ["try { Start-Process -FilePath $target -ErrorAction Stop } catch { }"] * start

    def failed(code: str, exit_code: int, remove_download: bool) -> list[str]:
        return [f"Report '{code}'", *(cleanup if remove_download else []), *restart, f"exit {exit_code}"]

    return "\n".join(
        [
            f"Start-Sleep -Seconds {delay_s}",
            *_status_function(status),
            f"$msi = {_quote(msi)}; $target = {_quote(target)}; $log = {_quote(log)}; $msiexec = {_msiexec(msiexec)}",
            *_wait_for_exe(tries),
            "if (-not $free) {",
            *[f"  {line}" for line in failed("busy", 1, True)],
            "}",
            # the download sat in the temp folder since it was verified
            "$actual = ''",
            "try { $actual = (Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash } catch { }",
            f"if ($actual -ne '{sha256.lower()}') {{",
            *[f"  {line}" for line in failed("verify", 3, True)],
            "}",
            "try {",
            "  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) -ErrorAction Stop | Out-Null",
            "} catch { }",
            "$rc = -1",
            "try {",
            "  $msiArgs = '/i \"' + $msi + '\" /qn /norestart /l*v \"' + $log + '\"'",
            "  $run = Start-Process -FilePath $msiexec -ArgumentList $msiArgs -Wait -PassThru -ErrorAction Stop",
            "  $rc = $run.ExitCode",
            "} catch { }",
            "if ($rc -ne 0 -and $rc -ne 3010) {",
            *[f"  {line}" for line in failed("msi", 5, False)],
            "}",
            *cleanup,
            *(["Remove-Item -LiteralPath $statusFile -Force -ErrorAction Ignore"] if status else []),
            *(["try { Start-Process -FilePath $target -ErrorAction Stop } catch { Report 'start'; exit 2 }"] * start),
            "exit 0",
        ]
    )


def _folder_removal(folders: list[Path]) -> list[str]:
    """PowerShell: delete each folder; one that is a junction or symlink is skipped (``$code`` = 'reparse')."""
    lines: list[str] = []
    for folder in folders:
        lines += [
            f"$f = Get-Item -LiteralPath {_quote(folder)} -Force -ErrorAction Ignore",
            "if ($f -and ($f.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { $code = 'reparse' }",
            "elseif ($f) { Remove-Item -LiteralPath $f.FullName -Recurse -Force -ErrorAction Ignore }",
        ]
    return lines


def msi_uninstall_script(
    exe: Path,
    product_code: str,
    folders: list[Path],
    delay_s: int = 1,
    tries: int = 120,
    status: Path | None = None,
    msiexec: Path | None = None,
) -> str:
    """Wait until ``exe`` is free, remove the installed program with msiexec, then delete the ``folders``.

    msiexec's 0 (removed), 1605 (not installed any more) and 3010 count as done. If it fails or ``exe`` stays busy,
    ``status`` gets the code 'uninstall', exit code 1, and the folders are left alone so that a second try can do all.
    A folder that is a junction or symlink is skipped, not followed; that and other leftovers give exit code 1 without a
    status code, since the status file would live in the folder that was just deleted.
    """
    _check_product_code(product_code)
    lines = [
        f"Start-Sleep -Seconds {delay_s}",
        *_status_function(status),
        f"$target = {_quote(exe)}; $msiexec = {_msiexec(msiexec)}",
        *_wait_for_exe(tries),
        "$rc = -1",
        "if ($free) {",
        "  try {",
        f"    $msiArgs = '/x {product_code} /qn /norestart'",
        "    $run = Start-Process -FilePath $msiexec -ArgumentList $msiArgs -Wait -PassThru -ErrorAction Stop",
        "    $rc = $run.ExitCode",
        "  } catch { }",
        "}",
        "if ($rc -ne 0 -and $rc -ne 1605 -and $rc -ne 3010) {",
        "  Report 'uninstall'",
        "  exit 1",
        "}",
        "$code = ''",
        *_folder_removal(folders),
    ]
    if folders:
        paths = ", ".join(_quote(p) for p in folders)
        lines += [f"if (@({paths}) | Where-Object {{ Test-Path -LiteralPath $_ }}) {{ exit 1 }}"]
    # a status file from an earlier failed run; one inside a deleted folder is gone already (Ignore: no error)
    lines += ["Remove-Item -LiteralPath $statusFile -Force -ErrorAction Ignore"] * (status is not None)
    return "\n".join([*lines, "exit 0"])


def _check_product_code(code: object) -> str:
    """``code`` if it is a GUID in braces, the only shape that may reach a script; UpdateError otherwise."""
    if not isinstance(code, str) or not _GUID.fullmatch(code):
        raise UpdateError(tr("update.err.bad_product_code"))
    return code


def uninstall_script(
    exe: Path | None, folders: list[Path], delay_s: int = 1, tries: int = 120, status: Path | None = None
) -> str:
    """Wait until ``exe`` can be deleted, then delete it, a leftover backup of an update and the ``folders``.

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
            f"Remove-Item -LiteralPath {_quote(_backup_path(exe))} -Force -ErrorAction Ignore",
        ]
    lines += _folder_removal(folders)
    paths = ", ".join(_quote(p) for p in [*([exe, _backup_path(exe)] if exe is not None else []), *folders])
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
    """Download ``release`` and schedule the swap. The caller must quit the app right afterwards.

    Only a release newer than this program is installed, so a correctly signed older one is no way back.
    A copy installed by the MSI loads the MSI of the release and runs it with msiexec instead of swapping the exe.
    """
    _require_newer(release.tag)
    if not can_self_update():
        raise UpdateError("Automatisch aktualisieren geht nur mit der heruntergeladenen MourseDecoder.exe.")
    target = Path(sys.executable)
    msi = install_kind() == "msi"
    # Windows Installer keeps a record of the files it put there; swapping the exe in its folder would corrupt it,
    # so an installed copy takes only the installer, and never falls back to the exe
    if not msi and not _can_write(target.parent):
        raise UpdateError(tr("update.err.no_write", folder=target.parent), open_page=True)
    folder = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        if msi:
            status = result_path()
            installer, digest = _download_msi(release, folder, urlopen)
            run_after_exit(msi_update_script(installer, target, status, digest, status.parent / MSI_LOG_FILE), popen)
        else:
            new_exe, digest = _download(release, folder, urlopen)
            run_after_exit(update_script(new_exe, target, status=result_path(), sha256=digest), popen)
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

    A copy installed by the MSI is removed by msiexec, using the ProductCode the installer wrote to the registry.
    The files are removed after this app has ended, so the caller must quit right afterwards.
    """
    folders = uninstall_targets(config_dir, delete_data)
    if install_kind() == "msi":  # the installer removes its own files; the exe is not deleted by hand
        code = _check_product_code(_registry_string("ProductCode"))
        run_after_exit(msi_uninstall_script(Path(sys.executable), code, folders, status=result_path(config_dir)), popen)
        return
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
