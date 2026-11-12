"""Backup of the configuration folder as one zip file.

The zip holds ``manifest.json`` plus ``settings.json``, ``function_keys.json`` and the ``*.json`` files of
``alphabets/``, ``smileys/`` and ``languages/``, byte for byte as they are on disk. ``telecode.sqlite`` is
left out unless asked for. The manifest lists every file with size and SHA-256.

An import validates the whole zip first (names, sizes, hashes, JSON, schema version, and the content with
the loaders the program itself uses) and writes nothing if anything is wrong. Before it writes, the current
files go into a restore point zip next to the config folder (the newest 5 are kept), so an import can be
undone by importing that zip. Files that are not in the bundle stay as they are.

Everything here may run in a worker thread: nothing touches Tk, and every failure leaves as ``BackupError``.
"""

from __future__ import annotations

import atexit
import dataclasses
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import unicodedata
import zipfile
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, backup_content, jsontext
from .backup_content import Note
from .function_keys import FILE_NAME as _FUNCTION_KEYS_NAME
from .function_keys import SCHEMA_VERSION as FUNCTION_KEYS_SCHEMA_VERSION
from .function_keys import FunctionKeys, FunctionKeysStore
from .import_gate import gate
from .settings import Settings, SettingsStore, is_valid_id

FORMAT_NAME = "mourse-decoder-backup"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
SETTINGS_FILE = SettingsStore.FILE_NAME
FUNCTION_KEYS_FILE = _FUNCTION_KEYS_NAME
TELECODE_FILE = "telecode.sqlite"
FOLDERS = ("alphabets", "smileys", "languages")

MiB = 1024 * 1024
MAX_ENTRIES = 2000
MAX_ENTRY_BYTES = 50 * MiB  # ceiling for every file; telecode.sqlite is the only one that can come near it
MAX_TOTAL_BYTES = 200 * MiB
MAX_ZIP_BYTES = MAX_TOTAL_BYTES + 16 * MiB  # the zip is read into memory once, see _read_zip
MAX_RATIO = 100  # uncompressed / compressed, only checked for entries above RATIO_FROM_BYTES
RATIO_FROM_BYTES = MiB
MAX_NAME_LENGTH = 120
# Per-file limits. Real files: settings.json about 0.5 KB (18 fields), shipped alphabets and smiley sets under
# 4 KB, language files about 20 KB. function_keys.json holds at most 100 slots with 10 000 macro characters
# each (1 MB, up to 6 MB if every character is written as \uXXXX). Hand-written alphabets with pasted texts
# of several 100 KB are the biggest thing users make (README: "Emoji and long texts"), so 2 MiB leaves room.
MAX_SETTINGS_BYTES = MiB
MAX_FUNCTION_KEYS_BYTES = 8 * MiB
MAX_USER_JSON_BYTES = 2 * MiB
MAX_MANIFEST_BYTES = 4 * MiB  # 2000 entries of about 250 bytes
KEEP_RESTORE_POINTS = 5
SQLITE_CHECK_SECONDS = 60
MACRO_SUMMARY_LENGTH = 200

_SQLITE_HEADER = b"SQLite format 3\x00"
_FILE_NAME = re.compile(r"[^\x00-\x1f\\/:*?\"<>|]+\.json\Z")
_RESERVED = {
    "con", "prn", "aux", "nul", "conin$", "conout$", "clock$",
    *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10)),
}
_SHORT_NAME = re.compile(r"~[0-9]")  # 8.3 names such as ALPHAB~1.json
# bidi controls and zero-width characters that hide what a name says; ZWJ and ZWNJ stay (real scripts need them)
_HIDING = {
    chr(c) for c in (0x061C, 0x200B, 0x200E, 0x200F, 0x2060, 0xFEFF, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
}
_TEMP_SUFFIX = ".mourse-tmp"


# Every failure has a stable code and parameters; the UI shows ``tr("backup.err.<code>", **params)``. The English
# text here is what ``str(error)`` says (logs, tests, callers without a UI) and the fallback of a missing key.
_MESSAGES = {
    "nothing_to_back_up": "There is nothing to back up: the config folder has no settings or user files.",
    "folder_too_large": "The config folder is too large for a backup bundle.",
    "export_failed": "The export failed: {error}",
    "file_too_large_export": "{name} is larger than {limit}, so a backup could not import it again.",
    "file_too_large": "{name} is larger than {limit}.",
    "cannot_read_folder": "Cannot read {name}: {error}",
    "cannot_read_file": "Cannot read {name}: {error}",
    "cannot_write": "Cannot write {name}: {error}",
    "zip_too_large": "{name} is larger than {limit}.",
    "zip_unreadable": "{name} is not a readable zip file: {error}",
    "entry_unreadable": "Cannot read {name} from the zip: {error}",
    "entry_size_mismatch": "{name}: the size in the zip does not match its content.",
    "too_many_entries": "The zip has more than {count} entries.",
    "backslash": "Backslashes are not allowed in names in the zip: {name}",
    "folder_entry": "Unexpected folder entry in the zip: {name}",
    "encrypted": "{name} is encrypted.",
    "symlink": "{name} is a symbolic link.",
    "bad_name": "Unexpected or unsafe name in the zip: {name}",
    "duplicate_entry": "{name} appears twice in the zip.",
    "bomb": "{name} is compressed suspiciously well (zip bomb?).",
    "total_too_large": "The zip holds more than {limit} when unpacked.",
    "manifest_json": "manifest.json is not valid JSON: {error}",
    "not_a_backup": "manifest.json does not describe a Mourse Decoder backup.",
    "format_version": "Unknown backup format version {version}; this program reads version {supported}.",
    "manifest_files": "manifest.json has no valid file list.",
    "manifest_missing": "manifest.json is missing, this is not a Mourse Decoder backup.",
    "manifest_bad_name": "manifest.json lists an unexpected or unsafe name: {name}",
    "manifest_duplicate": "{name} is listed twice in manifest.json.",
    "manifest_missing_file": "{name} is listed in manifest.json but missing in the zip.",
    "checksum": "{name} does not match its checksum; the zip is damaged or was changed.",
    "unlisted": "The zip holds files that manifest.json does not list: {name}",
    "no_files": "The backup contains no files.",
    "file_unusable": "{name} is not usable: {reason}",
    "newer_version": "{name} comes from a newer program version (schema_version {version}, this one knows "
    "{supported}). Update the program first.",
    "not_sqlite": "{name} is not an SQLite database.",
    "sqlite_damaged": "{name} is damaged: {reason}",
    "sqlite_schema": "{name} has no telecode table with the columns code and char.",
    "sqlite_error": "{name} is not a usable database: {error}",
    "clash": "The function keys and the settings would clash: {details}",
    "not_a_plain_file": "{name} in the config folder is not a plain file, nothing was changed.",
    "cannot_read_current": "Cannot read {name} in the config folder: {error}",
    "check_failed": "The check of the backup failed: {error}",
    "changed_after_check": "The zip file changed after it was checked. Nothing was imported; choose it again.",
    "unknown_mode": "Unknown import mode {mode}.",
    "import_failed": "The import failed: {error}",
    "link_folder": "{name} in the config folder is a link, nothing was changed.",
    "outside_folder": "{name} would be written outside the config folder, nothing was changed.",
    "write_failed": "Writing {name} failed ({error}); the config folder was left as it was.",
    "write_failed_partial": "Writing {name} failed ({error}). These files could not be put back and may be in the "
    "imported state: {files}.",
    "write_failed_partial_point": "Writing {name} failed ({error}). These files could not be put back and may be in "
    "the imported state: {files}. Your previous files are in the restore point {restore_point}; import that zip to "
    "repair them.",
    "busy": "An import is running. Try again when it is finished.",
    "sqlite_unexpected": "{name} holds something the program does not use ({what}); only the table telecode "
    "is allowed.",
    "settings_bad_id": "settings.json: {field} is no valid id ({value}); an id is a file name (a-z, 0-9, - and _).",
    "function_keys_invalid": "function_keys.json cannot be installed: {details}",
}


class BackupError(Exception):
    """The bundle or the config folder cannot be used.

    ``code`` names the failure (a key of ``_MESSAGES``, ``backup.err.<code>`` in the language files) and ``params``
    fill its placeholders. A code that is not known is taken as free text (code ``"other"``). ``restore_point`` is
    the restore point zip if one had been written when the error happened, ``path`` the bundle file the problem
    is in (e.g. ``function_keys.json``), if there is one.
    """

    def __init__(self, code: str, *, restore_point: Path | None = None, path: str | None = None, **params: object):
        template = _MESSAGES.get(code)
        self.code = code if template is not None else "other"
        self.params = {k: str(v) for k, v in params.items()}
        if restore_point is not None:
            self.params["restore_point"] = str(restore_point)
        super().__init__(template.format(**self.params) if template is not None else code)
        self.restore_point = restore_point
        self.path = path


@dataclass(frozen=True)
class PreviewEntry:
    path: str
    status: str  # "add", "replace", "unchanged" or "remove" (a restore point takes back what an import created)
    size: int
    actual: str = ""  # the name on disk if it differs from ``path`` only in case (Windows, macOS)

    @property
    def label(self) -> str:
        """The path as it may be shown: no control, bidi or zero-width characters."""
        return display_text(self.actual or self.path)


@dataclass(frozen=True)
class SlotSummary:
    """An enabled function key slot with a macro, as the import would install it."""

    slot: int
    trigger: str
    enabled: bool
    macro: str  # shortened and escaped for display
    risks: tuple[str, ...]  # what it does besides typing, from macros.commands_in_texts, plus a risky trigger


@dataclass(frozen=True)
class CommandSummary:
    """A code of an alphabet or smiley file whose text runs commands when it is keyed."""

    path: str
    code: str
    text: str  # shortened and escaped for display
    risks: tuple[str, ...]


@dataclass(frozen=True)
class Preview:
    manifest: dict[str, Any]
    entries: tuple[PreviewEntry, ...]
    sha256: str = ""  # of the zip bytes that were validated; pass it to import_bundle
    slots: tuple[SlotSummary, ...] = ()  # only when function_keys.json would change
    commands: tuple[CommandSummary, ...] = ()  # commands in the alphabet and smiley files that would change
    warnings: tuple[str, ...] = ()  # what the loaders of the program said (their own, German, text)
    notes: tuple[Note, ...] = ()  # references that will not exist after the import
    risky_files: tuple[str, ...] = ()  # files with macros or commands: what "import without macros" leaves out
    safe: Preview | None = None  # the preview of the import without ``risky_files``, checked on its own
    safe_error: BackupError | None = None  # why that import is not possible

    def count(self, status: str) -> int:
        return sum(1 for e in self.entries if e.status == status)

    @property
    def risky(self) -> bool:
        return any(s.risks for s in self.slots) or any(c.risks for c in self.commands)


@dataclass(frozen=True)
class ImportResult:
    restore_point: Path | None
    written: tuple[str, ...]
    unchanged: tuple[str, ...]
    removed: tuple[str, ...] = ()


# names and display


def display_text(text: str, limit: int | None = None) -> str:
    """``text`` with control, bidi and zero-width characters written out (``\\u202e``), cut at ``limit``."""
    out: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char in ("\n", "\r", "\t"):
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
        elif category in ("Cc", "Cs", "Zl", "Zp") or char in _HIDING or category == "Cf":
            out.append(f"\\u{ord(char):04x}" if ord(char) < 0x10000 else f"\\U{ord(char):08x}")
        else:
            out.append(char)
    shown = "".join(out)
    if limit is not None and len(shown) > limit:
        shown = shown[:limit] + "…"
    return shown


def _reserved(stem: str) -> bool:
    """A Windows device name, also in the spellings NFKC maps to one (``COM¹``, full-width ``CON``)."""
    base = unicodedata.normalize("NFKC", stem).casefold().split(".")[0].rstrip()
    if base in _RESERVED:
        return True
    # a superscript digit after a device name (``con³``) is how Windows and other tools disagree: refuse it too
    return any(c in "¹²³" for c in stem) and re.sub(r"[0-9]+\Z", "", base) in _RESERVED


def _safe_json_name(name: str) -> bool:
    stem = name[: -len(".json")]
    folded = unicodedata.normalize("NFKC", stem).casefold()
    return (
        bool(_FILE_NAME.fullmatch(name))
        and len(name) <= MAX_NAME_LENGTH
        and not name.startswith(".")
        and stem == stem.rstrip(" .")
        and not _reserved(stem)
        and not _SHORT_NAME.search(folded)
        and not any(c in _HIDING or unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in name)
    )


def is_allowed_name(path: str) -> bool:
    """True for the paths a bundle may hold (without the manifest)."""
    if path in (SETTINGS_FILE, FUNCTION_KEYS_FILE, TELECODE_FILE):
        return True
    folder, sep, name = path.partition("/")
    return bool(sep) and folder in FOLDERS and _safe_json_name(name)


def _limit(name: str) -> int:
    """Largest size a bundle file may have."""
    if name == MANIFEST:
        kind = MAX_MANIFEST_BYTES
    elif name == SETTINGS_FILE:
        kind = MAX_SETTINGS_BYTES
    elif name == FUNCTION_KEYS_FILE:
        kind = MAX_FUNCTION_KEYS_BYTES
    elif name == TELECODE_FILE:
        kind = MAX_ENTRY_BYTES
    else:
        kind = MAX_USER_JSON_BYTES
    return min(kind, MAX_ENTRY_BYTES)


def _size_text(size: int) -> str:
    return f"{size // MiB} MiB" if size >= MiB else f"{size} bytes"


def _too_large(name: str) -> BackupError:
    return BackupError("file_too_large", name=name, limit=_size_text(_limit(name)), path=name)


def _schema_version(name: str, data: bytes) -> int | None:
    """The ``schema_version`` of a JSON file that has one; ValueError if it is unusable."""
    if name != FUNCTION_KEYS_FILE:
        return None
    version = _parse_json(data).get("schema_version", FUNCTION_KEYS_SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("schema_version ist keine positive ganze Zahl")
    return version


def _parse_json(data: bytes) -> dict[str, Any]:
    try:
        parsed = jsontext.loads(data.decode("utf-8-sig"), dict)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ValueError("the top level is not an object")
    return parsed


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# collecting files of the config folder


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _regular(path: Path, root: Path) -> bool:
    return path.is_file() and not path.is_symlink() and _inside(path, root)


def _collect(config_dir: Path, include_telecode: bool) -> list[str]:
    """Bundle paths of the files in the config folder (sorted folders, fixed top-level order)."""
    names: list[str] = []
    top = [SETTINGS_FILE, FUNCTION_KEYS_FILE] + ([TELECODE_FILE] if include_telecode else [])
    names += [n for n in top if _regular(config_dir / n, config_dir)]
    for folder in FOLDERS:
        directory = config_dir / folder
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            entries = sorted(p.name for p in directory.iterdir())
        except OSError as exc:
            raise BackupError("cannot_read_folder", name=directory, error=exc) from exc
        names += [f"{folder}/{n}" for n in entries if _safe_json_name(n) and _regular(directory / n, config_dir)]
    return names


def _read(config_dir: Path, name: str) -> bytes:
    path = config_dir / name
    try:
        if path.stat().st_size > _limit(name):
            raise BackupError("file_too_large_export", name=name, limit=_size_text(_limit(name)), path=name)
        return path.read_bytes()
    except OSError as exc:
        raise BackupError("cannot_read_file", name=name, error=exc) from exc


def _manifest(files: dict[str, bytes], restore_point: bool = False, created: list[str] | None = None) -> dict[str, Any]:
    entries = []
    for name, data in files.items():
        entry: dict[str, Any] = {"path": name, "size": len(data), "sha256": _sha256(data)}
        if name.endswith(".json"):
            try:
                version = _schema_version(name, data)
            except ValueError:
                version = None  # an already broken file is saved as it is; the import decides
            if version is not None:
                entry["schema_version"] = version
        entries.append(entry)
    manifest: dict[str, Any] = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "app_version": __version__,
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contains_telecode": TELECODE_FILE in files,
        "files": entries,
    }
    if restore_point:
        manifest["restore_point"] = True  # what the clean-up of old restore points looks for
        # files that did not exist when the import ran and were created by it: importing the restore point removes
        # them again, otherwise "undo" would leave them behind
        manifest["restore_created"] = list(created or [])
    return manifest


def _open_private(path: Path):
    """New file for writing that only the owner may read (mode 0o600 where the OS has modes).

    On Windows the mode is ignored: the file inherits the access list of its folder (the user's profile).
    """
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    return os.fdopen(fd, "wb")


def _write_zip(
    zip_path: Path, files: dict[str, bytes], restore_point: bool = False, created: list[str] | None = None
) -> dict[str, Any]:
    manifest = _manifest(files, restore_point, created)
    zip_path = Path(zip_path)
    tmp = zip_path.with_name(zip_path.name + _TEMP_SUFFIX)
    try:
        with _open_private(tmp) as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
            for name, data in files.items():
                archive.writestr(name, data)
        os.replace(tmp, zip_path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError("cannot_write", name=zip_path, error=exc) from exc
    except BaseException:  # not an OSError (a zip library error, Ctrl+C): the half-written temp file must not stay
        tmp.unlink(missing_ok=True)
        raise
    return manifest


def export_bundle(config_dir: Path, zip_path: Path, include_telecode: bool = False) -> dict[str, Any]:
    """Write the config files to ``zip_path`` and return the manifest.

    ``telecode.sqlite`` is only added with ``include_telecode``: the app cannot rebuild it by itself (it
    needs the Unihan download and ``mourse-decoder-build-telecodes``), but it holds no user data.
    """
    try:
        config_dir = Path(config_dir)
        names = _collect(config_dir, include_telecode)
        if not names:
            raise BackupError("nothing_to_back_up")
        files = {name: _read(config_dir, name) for name in names}
        if len(files) + 1 > MAX_ENTRIES or sum(map(len, files.values())) > MAX_TOTAL_BYTES:
            raise BackupError("folder_too_large")
        return _write_zip(Path(zip_path), files)
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001 - whatever the disk or the zip library does, the UI gets one type
        raise BackupError("export_failed", error=f"{type(exc).__name__}: {exc}") from exc


# reading and validating a bundle


def _read_zip(zip_path: Path) -> bytes:
    """The bytes of the zip. They are read once and everything else works on them, so the file cannot
    change between the check and the import."""
    path = Path(zip_path)
    try:
        if path.stat().st_size > MAX_ZIP_BYTES:
            raise BackupError("zip_too_large", name=path.name, limit=f"{MAX_ZIP_BYTES // MiB} MiB")
        return path.read_bytes()
    except OSError as exc:
        raise BackupError("zip_unreadable", name=path.name, error=exc) from exc


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Read an entry without trusting the declared size."""
    limit = min(info.file_size, MAX_ENTRY_BYTES)
    try:
        with archive.open(info) as stream:
            data = stream.read(limit + 1)
    except Exception as exc:  # noqa: BLE001 - zlib.error, LZMAError, BadZipFile, EOFError, NotImplementedError, ...
        raise BackupError(
            "entry_unreadable", name=display_text(info.filename), error=f"{type(exc).__name__}: {exc}"
        ) from exc
    if len(data) != info.file_size:
        raise BackupError("entry_size_mismatch", name=display_text(info.filename))
    return data


def _check_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise BackupError("too_many_entries", count=MAX_ENTRIES)
    seen: set[str] = set()
    total = 0
    kept: list[zipfile.ZipInfo] = []
    for info in infos:
        name = info.filename
        # zipfile turns "\" into "/" on Windows, so the name as it stands in the file is checked too
        if "\\" in name or "\\" in info.orig_filename:
            raise BackupError("backslash", name=display_text(info.orig_filename))
        if info.is_dir():
            if name.rstrip("/") in FOLDERS:
                continue
            raise BackupError("folder_entry", name=display_text(name))
        if info.flag_bits & 0x1:
            raise BackupError("encrypted", name=display_text(name))
        if stat.S_ISLNK(info.external_attr >> 16):
            raise BackupError("symlink", name=display_text(name))
        if name != MANIFEST and not is_allowed_name(name):
            raise BackupError("bad_name", name=display_text(name))
        if name.casefold() in seen:
            raise BackupError("duplicate_entry", name=display_text(name))
        seen.add(name.casefold())
        if info.file_size > _limit(name):
            raise _too_large(name)
        if info.file_size > RATIO_FROM_BYTES and info.file_size > info.compress_size * MAX_RATIO:
            raise BackupError("bomb", name=display_text(name))
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise BackupError("total_too_large", limit=f"{MAX_TOTAL_BYTES // MiB} MiB")
        kept.append(info)
    return kept


def _load_manifest(data: bytes) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BackupError("manifest_json", error=exc) from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
        raise BackupError("not_a_backup")
    version = manifest.get("format_version")
    if isinstance(version, bool) or version != FORMAT_VERSION:
        raise BackupError("format_version", version=display_text(repr(version), 40), supported=FORMAT_VERSION)
    files = manifest.get("files")
    if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
        raise BackupError("manifest_files")
    return files


def _load_bundle(data: bytes, source: str = "The file") -> tuple[dict[str, Any], dict[str, bytes]]:
    """(manifest, files by bundle path) with names, sizes and hashes checked. Raises BackupError, writes nothing."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - BadZipFile, OSError, struct.error, OverflowError, ...
        raise BackupError("zip_unreadable", name=source, error=exc) from exc
    with archive:
        infos = _check_entries(archive)
        by_name = {i.filename: i for i in infos}
        if MANIFEST not in by_name:
            raise BackupError("manifest_missing")
        manifest_bytes = _read_entry(archive, by_name[MANIFEST])
        listed = _load_manifest(manifest_bytes)
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
        files: dict[str, bytes] = {}
        for entry in listed:
            name = entry.get("path")
            if not isinstance(name, str) or not is_allowed_name(name):
                raise BackupError("manifest_bad_name", name=display_text(str(name)))
            if name in files:
                raise BackupError("manifest_duplicate", name=name)
            if name not in by_name:
                raise BackupError("manifest_missing_file", name=name)
            content = _read_entry(archive, by_name[name])
            if entry.get("size") != len(content) or entry.get("sha256") != _sha256(content):
                raise BackupError("checksum", name=name)
            files[name] = content
        unlisted = sorted(set(by_name) - set(files) - {MANIFEST})
        if unlisted:
            raise BackupError("unlisted", name=display_text(unlisted[0]))
    created = manifest.get("restore_created", [])
    if not isinstance(created, list) or not all(isinstance(n, str) and is_allowed_name(n) for n in created):
        raise BackupError("manifest_files")
    if not files and not created:
        raise BackupError("no_files")
    return manifest, files


_live_scratch: set[Path] = set()  # folders that exist right now; the exit hook removes what is left


def _clean_scratch() -> None:
    """At the end of the process (a thread that is killed with it never reaches its ``finally``)."""
    for path in list(_live_scratch):
        shutil.rmtree(path, ignore_errors=True)
        _live_scratch.discard(path)


atexit.register(_clean_scratch)


@contextmanager
def _scratch():
    """A temporary folder outside the config folder; removed at the end, also after an error."""
    path = Path(tempfile.mkdtemp(prefix="mourse-backup-"))
    _live_scratch.add(path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        _live_scratch.discard(path)


class _Warnings(logging.Handler):
    """Collects what the settings loader logs while it runs (it logs instead of raising)."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []
        self._thread = threading.get_ident()

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self._thread:
            self.messages.append(record.getMessage())


def _accept_any(_combo: str) -> None:
    """Stands in for the injector's check when there is none: macros are still parsed, key names are not."""


def _load_settings(data: bytes | None) -> tuple[Settings, list[str]]:
    """The settings the program would end up with, by running ``SettingsStore.load`` on a copy of the file.

    BackupError if the loader would throw the file away (it moves such a file to ``.bak`` and uses defaults).
    """
    if data is None:
        return Settings(), []
    with _scratch() as folder:
        (folder / SETTINGS_FILE).write_bytes(data)
        handler = _Warnings()
        logger = logging.getLogger(SettingsStore.__module__)
        logger.addHandler(handler)
        try:
            settings = SettingsStore(folder).load()
        finally:
            logger.removeHandler(handler)
        if not (folder / SETTINGS_FILE).exists():
            reason = handler.messages[0] if handler.messages else ""
            match = re.match(r"[^(]*\((.*)\) – ", reason, re.S)
            raise BackupError(
                "file_unusable", name=SETTINGS_FILE, reason=match[1] if match else reason or "invalid content",
                path=SETTINGS_FILE,
            )
        return settings, handler.messages


def _load_function_keys(
    data: bytes | None, check_shortcut: Callable[[str], None] | None
) -> tuple[FunctionKeys, list[str]]:
    """The slots the program would end up with (``FunctionKeysStore.load``, macros parsed) and its warnings."""
    if data is None:
        return FunctionKeys(), []
    with _scratch() as folder:
        (folder / FUNCTION_KEYS_FILE).write_bytes(data)
        store = FunctionKeysStore(folder)
        keys, warnings = store.load(check_shortcut or _accept_any)
        if not (folder / FUNCTION_KEYS_FILE).exists():  # unreadable: the loader moved it and used defaults
            raise BackupError(
                "file_unusable", name=FUNCTION_KEYS_FILE, reason=warnings[0] if warnings else "invalid content",
                path=FUNCTION_KEYS_FILE,
            )
        return keys, warnings


def _check_sqlite(data: bytes) -> None:
    """Open a copy read-only in a temp folder and check structure; the config folder is never touched.

    Only the table ``telecode`` (and the index SQLite makes for its key) may exist: a view, trigger or virtual
    table with that name could run an endless query under the lock of the program's lookup. A time limit stops
    a check that is made to run for long.
    """
    if not data.startswith(_SQLITE_HEADER):
        raise BackupError("not_sqlite", name=TELECODE_FILE, path=TELECODE_FILE)
    with _scratch() as folder:
        copy = folder / TELECODE_FILE
        copy.write_bytes(data)
        conn = None
        try:
            conn = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro", uri=True)
            conn.execute("PRAGMA trusted_schema = OFF")
            deadline = time.monotonic() + SQLITE_CHECK_SECONDS
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            rows = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
            for kind, name, sql in rows:
                virtual = (sql or "").lstrip().upper().startswith("CREATE VIRTUAL")
                if (kind == "table" and name == "telecode" and not virtual) or (
                    kind == "index" and str(name).startswith("sqlite_autoindex_telecode")
                ):
                    continue
                raise BackupError("sqlite_unexpected", name=TELECODE_FILE, what=f"{kind} {name}", path=TELECODE_FILE)
            result = conn.execute("PRAGMA quick_check").fetchall()
            if result != [("ok",)]:
                raise BackupError(
                    "sqlite_damaged", name=TELECODE_FILE, reason=result[0][0] if result else "no result",
                    path=TELECODE_FILE,
                )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(telecode)")}
            if not {"code", "char"} <= columns:
                raise BackupError("sqlite_schema", name=TELECODE_FILE, path=TELECODE_FILE)
        except sqlite3.Error as exc:
            raise BackupError("sqlite_error", name=TELECODE_FILE, error=exc, path=TELECODE_FILE) from exc
        finally:
            if conn is not None:
                conn.close()


def _validate_content(name: str, data: bytes) -> None:
    if name == TELECODE_FILE:
        _check_sqlite(data)
        return
    try:
        parsed_version = _schema_version(name, data) if name == FUNCTION_KEYS_FILE else None
        if parsed_version is None:
            _parse_json(data)
    except ValueError as exc:
        raise BackupError("file_unusable", name=name, reason=exc, path=name) from exc
    if parsed_version is not None and parsed_version > FUNCTION_KEYS_SCHEMA_VERSION:
        raise BackupError(
            "newer_version", name=name, version=parsed_version, supported=FUNCTION_KEYS_SCHEMA_VERSION, path=name
        )


def _current(config_dir: Path, name: str) -> bytes | None:
    """Bytes of a file of the config folder for the cross-check; None if there is none that can be read."""
    path = config_dir / name
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= _limit(name):
            return path.read_bytes()
    except OSError:
        pass
    return None


def _summaries(keys: FunctionKeys) -> tuple[SlotSummary, ...]:
    """Every enabled slot with a macro; the ones that do something besides typing come first."""
    out = []
    for slot in keys.slots:
        if not (slot.enabled and slot.macro.strip()):
            continue
        risks = list(backup_content.slot_commands(slot.macro))
        if risk := backup_content.trigger_risk(slot.trigger):
            risks.append(f"trigger {display_text(risk)}")
        out.append(
            SlotSummary(
                slot.slot, display_text(slot.trigger), slot.enabled, display_text(slot.macro, MACRO_SUMMARY_LENGTH),
                tuple(risks),
            )
        )
    return tuple(sorted(out, key=lambda s: not s.risks))  # stable: slot order inside both groups


def _command_summaries(files: dict[str, bytes], changed: set[str]) -> tuple[CommandSummary, ...]:
    out: list[CommandSummary] = []
    for name, data in files.items():
        if name in changed and name.split("/")[0] in ("alphabets", "smileys"):
            for code, text, commands in backup_content.commands_per_code(data):
                shown = display_text(text, MACRO_SUMMARY_LENGTH)
                out.append(CommandSummary(name, display_text(code, 40), shown, commands))
    return tuple(out)


@dataclass(frozen=True)
class _Checked:
    keys: FunctionKeys | None
    warnings: tuple[str, ...]
    notes: tuple[Note, ...]


def _copy_user_files(config_dir: Path, work: Path, removed: set[str]) -> None:
    """The user files of the config folder that stay, into ``work`` (alphabets/, smileys/, languages/)."""
    for folder in FOLDERS:
        (work / folder).mkdir(exist_ok=True)
        source = config_dir / folder
        if source.is_dir() and not source.is_symlink():
            try:
                for path in source.glob("*.json"):
                    keep = f"{folder}/{path.name}" not in removed
                    if keep and _safe_json_name(path.name) and _regular(path, config_dir):
                        (work / folder / path.name).write_bytes(path.read_bytes())
            except OSError:
                pass  # an unreadable file of the user's own is not this import's business


def _user_file_problem(name: str, work: Path) -> str | None:
    folder, _, file_name = name.partition("/")
    stem = file_name[: -len(".json")]
    if folder == "languages":
        return backup_content.language_problem(work / name)
    if not backup_content.has_loadable_id(stem):
        return None  # the program skips a file whose name is no id, whatever it holds
    if folder == "alphabets":
        return backup_content.alphabet_problem(stem, work / "alphabets")
    return backup_content.smiley_problem(stem, work / "smileys")


def _check_user_files(
    config_dir: Path, files: dict[str, bytes], removed: set[str], settings: Settings | None
) -> list[Note]:
    """Alphabets, smiley sets and languages of the bundle through the real loaders, in the state after the import.

    The files of the config folder that stay and the bundle's files are copied to a temp folder (a bundle file
    wins), so ``extends`` finds what it needs. A file that does not load would simply be skipped by the program,
    which is a note, unless it replaces a file that does load: then the import is refused (a good alphabet must
    not be lost to a broken one). Returns notes about that and about what ``settings`` names that will not exist
    afterwards.
    """
    notes: list[Note] = []
    with _scratch() as work, _scratch() as now:
        _copy_user_files(config_dir, work, removed)
        _copy_user_files(config_dir, now, set())
        for name, data in files.items():
            if name.split("/")[0] in FOLDERS:
                (work / name).write_bytes(data)
        for name in files:
            if name.split("/")[0] not in FOLDERS:
                continue
            problem = _user_file_problem(name, work)
            if not problem:
                continue
            reason = display_text(problem, 300)
            if (config_dir / name).is_file() and _user_file_problem(name, now) is None:
                raise BackupError("file_unusable", name=display_text(name), reason=reason, path=name)
            notes.append(Note("file_skipped", {"name": display_text(name), "reason": reason}))
        if settings is None:
            return notes
        stems = {
            folder: {p.stem for p in (work / folder).glob("*.json") if backup_content.has_loadable_id(p.stem)}
            for folder in ("alphabets", "smileys")
        }
        has_telecode = TELECODE_FILE in files or (
            TELECODE_FILE not in removed and _regular(config_dir / TELECODE_FILE, config_dir)
        )
        notes += backup_content.reference_notes(
            settings, stems["alphabets"], stems["smileys"], work / "languages", has_telecode
        )
    return notes


def _validate_files(
    config_dir: Path, files: dict[str, bytes], check_shortcut: Callable[[str], None] | None, removed: set[str]
) -> _Checked:
    """Content checks of every file that will be written, with the loaders the program uses."""
    for name, data in files.items():
        _validate_content(name, data)
    warnings: list[str] = []
    settings = keys = None
    if SETTINGS_FILE in files:
        settings, found = _load_settings(files[SETTINGS_FILE])
        warnings += found
        raw = _parse_json(files[SETTINGS_FILE])
        for field in ("alphabet", "smileys"):  # the loader would reset a bad id to its default: say no instead
            value = raw.get(field)
            if isinstance(value, str) and value and not is_valid_id(value):
                raise BackupError(
                    "settings_bad_id", field=field, value=display_text(value, 60), path=SETTINGS_FILE
                )
    if FUNCTION_KEYS_FILE in files:
        keys, found = _load_function_keys(files[FUNCTION_KEYS_FILE], check_shortcut)
        warnings += found
        problems = keys.validate()  # the same check the program makes before it installs the slots
        if problems:
            raise BackupError(
                "function_keys_invalid", details="; ".join(problems[:3]), path=FUNCTION_KEYS_FILE
            )
    if settings is not None or keys is not None:
        # a slot trigger that the Morse key or a shortcut also fires on is refused by the running program
        other_settings = settings or _load_settings_lenient(_current(config_dir, SETTINGS_FILE))
        other_keys = keys or _load_keys_lenient(_current(config_dir, FUNCTION_KEYS_FILE), check_shortcut)
        clashes = other_keys.clashes(other_settings)
        if clashes:
            raise BackupError(
                "clash", details="; ".join(clashes[:3]), path=FUNCTION_KEYS_FILE if keys is not None else SETTINGS_FILE
            )
    notes: list[Note] = []
    if any(name.split("/")[0] in FOLDERS for name in files) or SETTINGS_FILE in files or removed:
        effective = settings if SETTINGS_FILE in files else None
        if effective is None and removed:
            effective = _load_settings_lenient(_current(config_dir, SETTINGS_FILE))
        notes = _check_user_files(config_dir, files, removed, effective)
    return _Checked(keys, tuple(warnings), tuple(notes))


def _load_settings_lenient(data: bytes | None) -> Settings:
    """The current settings file as the program reads it; a broken one counts as defaults (never blocks)."""
    try:
        return _load_settings(data)[0]
    except BackupError:
        return Settings()


def _load_keys_lenient(data: bytes | None, check_shortcut: Callable[[str], None] | None) -> FunctionKeys:
    try:
        return _load_function_keys(data, check_shortcut)[0]
    except BackupError:
        return FunctionKeys()


def _status(config_dir: Path, name: str, data: bytes) -> str:
    path = config_dir / name
    if not path.exists() and not path.is_symlink():
        return "add"
    if path.is_symlink() or not path.is_file():
        raise BackupError("not_a_plain_file", name=display_text(name))
    try:
        current = path.read_bytes()
    except OSError as exc:
        raise BackupError("cannot_read_current", name=display_text(name), error=exc) from exc
    return "unchanged" if current == data else "replace"


def _actual_name(config_dir: Path, name: str) -> str:
    """The name of an existing file as it is on disk if that differs from ``name`` only in case, else "".

    Windows and macOS find ``mine.json`` for ``Mine.json``; writing to the bundle's spelling would rename the
    user's file, so the write goes to the name that is there.
    """
    path = config_dir / name
    if not path.exists():
        return ""
    try:
        names = [entry.name for entry in path.parent.iterdir()]
    except OSError:
        return ""
    if path.name in names:
        return ""
    for entry in names:
        if entry.casefold() == path.name.casefold():
            folder = name.rpartition("/")[0]
            return f"{folder}/{entry}" if folder else entry
    return ""


@dataclass(frozen=True)
class _Loaded:
    manifest: dict[str, Any]
    files: dict[str, bytes]  # what would be written (excluded files are gone)
    sha256: str
    keys: FunctionKeys | None
    warnings: tuple[str, ...]
    notes: tuple[Note, ...]
    removed: tuple[str, ...]  # files a restore point takes back: the ones its import had created


def _load(
    zip_path: Path,
    config_dir: Path,
    exclude: Iterable[str],
    expected_sha256: str | None,
    check_shortcut: Callable[[str], None] | None,
) -> _Loaded:
    data = _read_zip(Path(zip_path))
    digest = _sha256(data)
    if expected_sha256 is not None and digest != expected_sha256:
        raise BackupError("changed_after_check")
    manifest, files = _load_bundle(data, Path(zip_path).name)
    skipped = set(exclude)
    files = {name: content for name, content in files.items() if name not in skipped}
    removed = tuple(
        name
        for name in manifest.get("restore_created", [])
        if name not in skipped and name not in files and _regular(config_dir / name, config_dir)
    )
    checked = _validate_files(config_dir, files, check_shortcut, set(removed))
    return _Loaded(manifest, files, digest, checked.keys, checked.warnings, checked.notes, removed)


def _preview(config_dir: Path, loaded: _Loaded) -> Preview:
    entries = [
        PreviewEntry(name, _status(config_dir, name, data), len(data), _actual_name(config_dir, name))
        for name, data in loaded.files.items()
    ]
    for name in loaded.removed:
        entries.append(PreviewEntry(name, "remove", (config_dir / name).stat().st_size, _actual_name(config_dir, name)))
    changed = {e.path for e in entries if e.status in ("add", "replace")}
    slots = _summaries(loaded.keys) if loaded.keys is not None and FUNCTION_KEYS_FILE in changed else ()
    commands = _command_summaries(loaded.files, changed)
    risky = ([FUNCTION_KEYS_FILE] if slots else []) + sorted({c.path for c in commands})
    return Preview(
        loaded.manifest, tuple(entries), loaded.sha256, slots, commands, loaded.warnings, loaded.notes, tuple(risky)
    )


def inspect_bundle(
    zip_path: Path,
    config_dir: Path,
    exclude: Iterable[str] = (),
    check_shortcut: Callable[[str], None] | None = None,
) -> Preview:
    """Validate the bundle and say what an import would add, replace, remove or leave unchanged. Writes nothing.

    ``exclude`` names bundle files that are left out: they are neither checked nor shown. The preview holds the
    SHA-256 of the zip that was checked, a summary of the macros of function_keys.json and of the commands in
    alphabet and smiley files that would change, and (``safe``) the same checked on its own for the import
    without every file that has macros or commands, so that choice shows its own result before it is made.
    ``check_shortcut`` is the controller's key check; without it macros are parsed but key names are not checked.
    """
    try:
        config_dir = Path(config_dir)
        skipped = list(exclude)
        preview = _preview(config_dir, _load(Path(zip_path), config_dir, skipped, None, check_shortcut))
        if preview.risky_files:
            try:
                without = [*skipped, *preview.risky_files]
                loaded = _load(Path(zip_path), config_dir, without, preview.sha256, check_shortcut)
                safe, safe_error = _preview(config_dir, loaded), None
            except BackupError as exc:
                safe, safe_error = None, exc
            preview = dataclasses.replace(preview, safe=safe, safe_error=safe_error)
        return preview
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BackupError("check_failed", error=f"{type(exc).__name__}: {exc}") from exc


# import


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + _TEMP_SUFFIX)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_file(path: Path, data: bytes) -> None:
    _atomic_write(path, data)


def _restore_point_path(config_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = config_dir.parent / f"{config_dir.name}-backup-{stamp}"
    candidate, counter = base.with_name(base.name + ".zip"), 1
    while candidate.exists():
        counter += 1
        candidate = base.with_name(f"{base.name}-{counter}.zip")
    return candidate


def _is_restore_point(path: Path) -> bool:
    """A zip this feature wrote as restore point (its manifest says so), not just a file with a similar name."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ZIP_BYTES:
            return False
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(MANIFEST)
            if info.file_size > MAX_MANIFEST_BYTES:
                return False
            manifest = json.loads(archive.read(info).decode("utf-8-sig"))
    except Exception:  # noqa: BLE001 - anything unreadable is not ours
        return False
    return (
        isinstance(manifest, dict) and manifest.get("format") == FORMAT_NAME and manifest.get("restore_point") is True
    )


def prune_restore_points(
    config_dir: Path, keep: int = KEEP_RESTORE_POINTS, protect: Path | None = None
) -> list[Path]:
    """Delete all but the newest ``keep`` restore points of this config folder; returns what was deleted.

    Only files named ``<folder>-backup-<YYYYMMDD-HHMMSS>[-<n>].zip`` whose manifest marks them as restore
    point are touched; failures are ignored (an old zip that stays is harmless). "Newest" is by modification
    time, not by the local time in the name (a clock set back, the end of summer time), and ``protect`` (the
    restore point that was just written) is never deleted.
    """
    pattern = re.compile(re.escape(config_dir.name) + r"-backup-(\d{8}-\d{6})(?:-(\d+))?\.zip\Z")
    found: list[tuple[int, str, Path]] = []
    try:
        for path in config_dir.parent.iterdir():
            if pattern.fullmatch(path.name):
                found.append((path.stat().st_mtime_ns, path.name, path))
    except OSError:
        return []
    kept: set[Path] = {protect} if protect is not None else set()
    for _mtime, _name, path in sorted(found, reverse=True):
        if len(kept) < keep:
            kept.add(path)
    deleted: list[Path] = []
    for _mtime, _name, path in found:
        if path not in kept and _is_restore_point(path):
            try:
                path.unlink()
                deleted.append(path)
            except OSError:
                pass
    return deleted


class _Progress:
    restore_point: Path | None = None


def import_bundle(
    zip_path: Path,
    config_dir: Path,
    mode: str = "replace",
    exclude: Iterable[str] = (),
    expected_sha256: str | None = None,
    check_shortcut: Callable[[str], None] | None = None,
) -> ImportResult:
    """Apply a bundle. ``replace`` overwrites the files that are in the bundle and leaves all others.

    Everything is validated before the first write. The current files go to a restore point zip
    (``<config folder>-backup-<time>.zip`` next to the folder, same format, importable again; the newest 5 are
    kept); it also lists the files this import creates, so importing it removes them again. If a write fails,
    the files already written are put back and the error is raised; if some cannot be put back, the error says
    which and names the restore point. ``exclude`` leaves bundle files out (they stay untouched and are not
    checked). With ``expected_sha256`` (from ``inspect_bundle``) the import is refused if the zip is not the
    one that was checked.
    """
    if mode != "replace":
        raise BackupError("unknown_mode", mode=display_text(repr(mode), 40))
    progress = _Progress()
    try:
        return _import(Path(zip_path), Path(config_dir), exclude, expected_sha256, check_shortcut, progress)
    except BackupError as exc:
        if exc.restore_point is None and progress.restore_point is not None:
            exc.restore_point = progress.restore_point
        raise
    except Exception as exc:  # noqa: BLE001
        raise BackupError(
            "import_failed", error=f"{type(exc).__name__}: {exc}", restore_point=progress.restore_point
        ) from exc


def _import(
    zip_path: Path,
    config_dir: Path,
    exclude: Iterable[str],
    expected_sha256: str | None,
    check_shortcut: Callable[[str], None] | None,
    progress: _Progress,
) -> ImportResult:
    loaded = _load(zip_path, config_dir, exclude, expected_sha256, check_shortcut)
    preview = _preview(config_dir, loaded)
    files = loaded.files
    todo = [e.path for e in preview.entries if e.status in ("add", "replace")]
    gone = [e.path for e in preview.entries if e.status == "remove"]
    unchanged = tuple(e.path for e in preview.entries if e.status == "unchanged")
    if not todo and not gone:
        return ImportResult(None, (), unchanged)

    for name in [*todo, *gone]:  # a folder that is a link would send the write somewhere else
        parent = (config_dir / name).parent
        if parent != config_dir and parent.is_symlink():
            raise BackupError("link_folder", name=display_text(parent.name))
        if not _inside(parent, config_dir) and parent.exists():
            raise BackupError("outside_folder", name=display_text(name))
    where = {e.path: config_dir / (e.actual or e.path) for e in preview.entries}  # the spelling that is on disk

    current = _collect(config_dir, include_telecode=TELECODE_FILE in files or TELECODE_FILE in gone)
    created = [e.path for e in preview.entries if e.status == "add"]
    restore_point = _restore_point_path(config_dir)
    restore_point.parent.mkdir(parents=True, exist_ok=True)
    _write_zip(
        restore_point, {name: _read(config_dir, name) for name in current}, restore_point=True, created=created
    )
    progress.restore_point = restore_point

    created_dirs: list[Path] = []
    done: list[tuple[str, Path]] = []
    originals: dict[str, bytes] = {}
    taken: dict[str, bytes] = {}  # files removed, to put back if something fails
    name = (todo or gone)[0]
    try:
        originals.update({e.path: where[e.path].read_bytes() for e in preview.entries if e.status == "replace"})
        taken.update({n: where[n].read_bytes() for n in gone})
        for name in todo:
            target = where[name]
            if not target.parent.exists():
                target.parent.mkdir(parents=True)
                created_dirs.append(target.parent)
            _write_file(target, files[name])
            done.append((name, target))
        for name in gone:
            where[name].unlink()
            done.append((name, where[name]))
    except BaseException as exc:
        failed = _roll_back(done, originals, taken, created_dirs)
        if not isinstance(exc, Exception):
            raise
        raise _failure(name, exc, failed, restore_point) from exc
    for name in gone:  # an empty folder that only held removed files goes too
        for parent in where[name].parents:
            if parent == config_dir or not parent.is_relative_to(config_dir):
                break
            try:
                parent.rmdir()
            except OSError:
                break
    prune_restore_points(config_dir, protect=restore_point)
    return ImportResult(restore_point, tuple(todo), unchanged, tuple(gone))


def _failure(name: str, exc: Exception, failed: list[str], restore_point: Path | None) -> BackupError:
    params: dict[str, object] = {"name": display_text(name), "error": exc}
    if not failed:
        return BackupError("write_failed", restore_point=restore_point, **params)
    params["files"] = ", ".join(display_text(f) for f in failed)
    if restore_point is None:
        return BackupError("write_failed_partial", **params)
    return BackupError("write_failed_partial_point", restore_point=restore_point, **params)


def _roll_back(
    done: list[tuple[str, Path]], originals: dict[str, bytes], taken: dict[str, bytes], created_dirs: list[Path]
) -> list[str]:
    """Undo the writes and removals in ``done``. Returns what could not be undone, as ``name (error)``."""
    failed: list[str] = []
    for name, target in done:
        try:
            if name in taken:
                _atomic_write(target, taken[name])  # a file that was removed comes back
            elif name in originals:
                _atomic_write(target, originals[name])
            else:
                target.unlink(missing_ok=True)
        except OSError as exc:
            failed.append(f"{name} ({exc})")
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
        except OSError as exc:
            if directory.exists():
                failed.append(f"{directory.name}/ ({exc})")
    return failed


__all__ = [
    "BackupError",
    "ImportResult",
    "Preview",
    "PreviewEntry",
    "CommandSummary",
    "SlotSummary",
    "gate",
    "display_text",
    "export_bundle",
    "import_bundle",
    "inspect_bundle",
    "is_allowed_name",
    "prune_restore_points",
]
