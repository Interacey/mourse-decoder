"""Backup of the configuration folder as one zip file.

The zip holds ``manifest.json`` plus ``settings.json``, ``function_keys.json`` and the ``*.json`` files of
``alphabets/``, ``smileys/`` and ``languages/``, byte for byte as they are on disk. ``telecode.sqlite`` is
left out unless asked for. The manifest lists every file with size and SHA-256.

An import validates the whole zip first (names, sizes, hashes, JSON, schema version) and writes nothing
if anything is wrong. Before it writes, the current files go into a restore point zip next to the config
folder, so an import can be undone by importing that zip. Files that are not in the bundle stay as they are.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, jsontext
from .function_keys import SCHEMA_VERSION as FUNCTION_KEYS_SCHEMA_VERSION

FORMAT_NAME = "mourse-decoder-backup"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
SETTINGS_FILE = "settings.json"
FUNCTION_KEYS_FILE = "function_keys.json"
TELECODE_FILE = "telecode.sqlite"
FOLDERS = ("alphabets", "smileys", "languages")

MAX_ENTRIES = 2000
MAX_ENTRY_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_RATIO = 100  # uncompressed / compressed, only checked for entries above RATIO_FROM_BYTES
RATIO_FROM_BYTES = 1024 * 1024
MAX_NAME_LENGTH = 120

_SQLITE_HEADER = b"SQLite format 3\x00"
_FILE_NAME = re.compile(r"[^\x00-\x1f\\/:*?\"<>|]+\.json\Z")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
_TEMP_SUFFIX = ".mourse-tmp"


class BackupError(Exception):
    """The bundle or the config folder cannot be used; the message is meant for the user."""


@dataclass(frozen=True)
class PreviewEntry:
    path: str
    status: str  # "add", "replace" or "unchanged"
    size: int


@dataclass(frozen=True)
class Preview:
    manifest: dict[str, Any]
    entries: tuple[PreviewEntry, ...]

    def count(self, status: str) -> int:
        return sum(1 for e in self.entries if e.status == status)


@dataclass(frozen=True)
class ImportResult:
    restore_point: Path | None
    written: tuple[str, ...]
    unchanged: tuple[str, ...]


# names


def _safe_json_name(name: str) -> bool:
    stem = name[: -len(".json")]
    return (
        bool(_FILE_NAME.fullmatch(name))
        and len(name) <= MAX_NAME_LENGTH
        and not name.startswith(".")
        and stem == stem.rstrip(" .")
        and stem.split(".")[0].rstrip().lower() not in _RESERVED
    )


def is_allowed_name(path: str) -> bool:
    """True for the paths a bundle may hold (without the manifest)."""
    if path in (SETTINGS_FILE, FUNCTION_KEYS_FILE, TELECODE_FILE):
        return True
    folder, sep, name = path.partition("/")
    return bool(sep) and folder in FOLDERS and _safe_json_name(name)


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
            raise BackupError(f"Cannot read {directory}: {exc}") from exc
        names += [f"{folder}/{n}" for n in entries if _safe_json_name(n) and _regular(directory / n, config_dir)]
    return names


def _read(config_dir: Path, name: str) -> bytes:
    path = config_dir / name
    try:
        if path.stat().st_size > MAX_ENTRY_BYTES:
            raise BackupError(f"{name} is larger than {MAX_ENTRY_BYTES // (1024 * 1024)} MiB.")
        return path.read_bytes()
    except OSError as exc:
        raise BackupError(f"Cannot read {name}: {exc}") from exc


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
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
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "app_version": __version__,
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contains_telecode": TELECODE_FILE in files,
        "files": entries,
    }


def _write_zip(zip_path: Path, files: dict[str, bytes]) -> dict[str, Any]:
    manifest = _manifest(files)
    zip_path = Path(zip_path)
    tmp = zip_path.with_name(zip_path.name + _TEMP_SUFFIX)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
            for name, data in files.items():
                archive.writestr(name, data)
        os.replace(tmp, zip_path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"Cannot write {zip_path}: {exc}") from exc
    return manifest


def export_bundle(config_dir: Path, zip_path: Path, include_telecode: bool = False) -> dict[str, Any]:
    """Write the config files to ``zip_path`` and return the manifest.

    ``telecode.sqlite`` is only added with ``include_telecode``: the app cannot rebuild it by itself (it
    needs the Unihan download and ``mourse-decoder-build-telecodes``), but it holds no user data.
    """
    config_dir = Path(config_dir)
    names = _collect(config_dir, include_telecode)
    if not names:
        raise BackupError("There is nothing to back up: the config folder has no settings or user files.")
    files = {name: _read(config_dir, name) for name in names}
    if len(files) + 1 > MAX_ENTRIES or sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        raise BackupError("The config folder is too large for a backup bundle.")
    return _write_zip(Path(zip_path), files)


# reading and validating a bundle


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Read an entry without trusting the declared size."""
    limit = min(info.file_size, MAX_ENTRY_BYTES)
    try:
        with archive.open(info) as stream:
            data = stream.read(limit + 1)
    except (zipfile.BadZipFile, OSError, NotImplementedError, RuntimeError, EOFError) as exc:
        raise BackupError(f"Cannot read {info.filename} from the zip: {exc}") from exc
    if len(data) != info.file_size:
        raise BackupError(f"{info.filename}: the size in the zip does not match its content.")
    return data


def _check_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise BackupError(f"The zip has more than {MAX_ENTRIES} entries.")
    seen: set[str] = set()
    total = 0
    kept: list[zipfile.ZipInfo] = []
    for info in infos:
        name = info.filename
        if info.is_dir():
            if name.rstrip("/") in FOLDERS:
                continue
            raise BackupError(f"Unexpected folder entry in the zip: {name!r}")
        if info.flag_bits & 0x1:
            raise BackupError(f"{name!r} is encrypted.")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise BackupError(f"{name!r} is a symbolic link.")
        if name != MANIFEST and not is_allowed_name(name):
            raise BackupError(f"Unexpected or unsafe name in the zip: {name!r}")
        if name.casefold() in seen:
            raise BackupError(f"{name!r} appears twice in the zip.")
        seen.add(name.casefold())
        if info.file_size > MAX_ENTRY_BYTES:
            raise BackupError(f"{name!r} is larger than {MAX_ENTRY_BYTES // (1024 * 1024)} MiB.")
        if info.file_size > RATIO_FROM_BYTES and info.file_size > info.compress_size * MAX_RATIO:
            raise BackupError(f"{name!r} is compressed suspiciously well (zip bomb?).")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise BackupError(f"The zip holds more than {MAX_TOTAL_BYTES // (1024 * 1024)} MiB when unpacked.")
        kept.append(info)
    return kept


def _load_manifest(data: bytes) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BackupError(f"{MANIFEST} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
        raise BackupError(f"{MANIFEST} does not describe a Mourse Decoder backup.")
    version = manifest.get("format_version")
    if isinstance(version, bool) or version != FORMAT_VERSION:
        raise BackupError(f"Unknown backup format version {version!r}; this program reads version {FORMAT_VERSION}.")
    files = manifest.get("files")
    if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
        raise BackupError(f"{MANIFEST} has no valid file list.")
    return files


def _load_bundle(zip_path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Fully validated (manifest, files by bundle path). Raises BackupError, writes nothing."""
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError(f"{Path(zip_path).name} is not a readable zip file: {exc}") from exc
    with archive:
        infos = _check_entries(archive)
        by_name = {i.filename: i for i in infos}
        if MANIFEST not in by_name:
            raise BackupError(f"{MANIFEST} is missing, this is not a Mourse Decoder backup.")
        manifest_bytes = _read_entry(archive, by_name[MANIFEST])
        listed = _load_manifest(manifest_bytes)
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
        files: dict[str, bytes] = {}
        for entry in listed:
            name = entry.get("path")
            if not isinstance(name, str) or not is_allowed_name(name):
                raise BackupError(f"{MANIFEST} lists an unexpected or unsafe name: {name!r}")
            if name in files:
                raise BackupError(f"{name!r} is listed twice in {MANIFEST}.")
            if name not in by_name:
                raise BackupError(f"{name!r} is listed in {MANIFEST} but missing in the zip.")
            data = _read_entry(archive, by_name[name])
            if entry.get("size") != len(data) or entry.get("sha256") != _sha256(data):
                raise BackupError(f"{name!r} does not match its checksum; the zip is damaged or was changed.")
            files[name] = data
        unlisted = sorted(set(by_name) - set(files) - {MANIFEST})
        if unlisted:
            raise BackupError(f"The zip holds files that {MANIFEST} does not list: {unlisted[0]!r}")
    if not files:
        raise BackupError("The backup contains no files.")
    for name, data in files.items():
        _validate_content(name, data)
    return manifest, files


def _validate_content(name: str, data: bytes) -> None:
    if name == TELECODE_FILE:
        if not data.startswith(_SQLITE_HEADER):
            raise BackupError(f"{name} is not an SQLite database.")
        return
    try:
        parsed_version = _schema_version(name, data) if name == FUNCTION_KEYS_FILE else None
        if parsed_version is None:
            _parse_json(data)
    except ValueError as exc:
        raise BackupError(f"{name} is not usable: {exc}") from exc
    if parsed_version is not None and parsed_version > FUNCTION_KEYS_SCHEMA_VERSION:
        raise BackupError(
            f"{name} comes from a newer program version (schema_version {parsed_version}, "
            f"this one knows {FUNCTION_KEYS_SCHEMA_VERSION}). Update the program first."
        )


def _status(config_dir: Path, name: str, data: bytes) -> str:
    path = config_dir / name
    if not path.exists() and not path.is_symlink():
        return "add"
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"{name} in the config folder is not a plain file, nothing was changed.")
    try:
        current = path.read_bytes()
    except OSError as exc:
        raise BackupError(f"Cannot read {name} in the config folder: {exc}") from exc
    return "unchanged" if current == data else "replace"


def _preview(config_dir: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> Preview:
    entries = tuple(PreviewEntry(name, _status(config_dir, name, data), len(data)) for name, data in files.items())
    return Preview(manifest, entries)


def inspect_bundle(zip_path: Path, config_dir: Path) -> Preview:
    """Validate the bundle and say what an import would add, replace or leave unchanged. Writes nothing."""
    manifest, files = _load_bundle(Path(zip_path))
    return _preview(Path(config_dir), manifest, files)


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


def import_bundle(zip_path: Path, config_dir: Path, mode: str = "replace") -> ImportResult:
    """Apply a bundle. ``replace`` overwrites the files that are in the bundle and leaves all others.

    Everything is validated before the first write. The current files go to a restore point zip
    (``<config folder>-backup-<time>.zip`` next to the folder, same format, importable again). If a write
    fails, the files already written are put back and the error is raised.
    """
    if mode != "replace":
        raise BackupError(f"Unknown import mode {mode!r}.")
    config_dir = Path(config_dir)
    manifest, files = _load_bundle(Path(zip_path))
    preview = _preview(config_dir, manifest, files)
    todo = [e.path for e in preview.entries if e.status != "unchanged"]
    unchanged = tuple(e.path for e in preview.entries if e.status == "unchanged")
    if not todo:
        return ImportResult(None, (), unchanged)

    for name in todo:  # a folder that is a link would send the write somewhere else
        parent = (config_dir / name).parent
        if parent != config_dir and parent.is_symlink():
            raise BackupError(f"{parent.name} in the config folder is a link, nothing was changed.")
        if not _inside(parent, config_dir) and parent.exists():
            raise BackupError(f"{name} would be written outside the config folder, nothing was changed.")

    restore_point: Path | None = None
    current = _collect(config_dir, include_telecode=TELECODE_FILE in files)
    if current:
        restore_point = _restore_point_path(config_dir)
        _write_zip(restore_point, {name: _read(config_dir, name) for name in current})

    originals = {e.path: (config_dir / e.path).read_bytes() for e in preview.entries if e.status == "replace"}
    created_dirs: list[Path] = []
    done: list[str] = []
    try:
        for name in todo:
            target = config_dir / name
            if not target.parent.exists():
                target.parent.mkdir(parents=True)
                created_dirs.append(target.parent)
            _write_file(target, files[name])
            done.append(name)
    except BaseException as exc:
        _roll_back(config_dir, done, originals, created_dirs)
        if isinstance(exc, OSError):
            raise BackupError(f"Writing {name} failed ({exc}); the config folder was left as it was.") from exc
        raise
    return ImportResult(restore_point, tuple(todo), unchanged)


def _roll_back(config_dir: Path, done: list[str], originals: dict[str, bytes], created_dirs: list[Path]) -> None:
    for name in done:
        target = config_dir / name
        try:
            if name in originals:
                _atomic_write(target, originals[name])
            else:
                target.unlink(missing_ok=True)
        except OSError:
            pass
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
        except OSError:
            pass


__all__ = [
    "BackupError",
    "ImportResult",
    "Preview",
    "PreviewEntry",
    "export_bundle",
    "import_bundle",
    "inspect_bundle",
    "is_allowed_name",
]
