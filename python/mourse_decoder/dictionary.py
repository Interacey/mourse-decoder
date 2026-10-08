"""Loading and managing alphabets.

An alphabet is a JSON file that maps Morse codes to characters and can inherit
from another one via ``extends`` (digits and punctuation live once in
``common.json``). Files are validated strictly on load so a typo shows up with
file name and reason instead of when someone happens to key that code.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .smileys import BUILTIN_SMILEY_DIR, SmileySetInfo, list_smiley_sets, load_smiley_codes

BUILTIN_ALPHABET_DIR = Path(__file__).with_name("alphabets")

# dots and dashes only, 1-32 symbols (32 = MAX_SEQUENCE_LEN in the Rust core)
_CODE_PATTERN = re.compile(r"^[.-]{1,32}$")
# ids double as file names
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class AlphabetError(ValueError):
    """Invalid or unloadable alphabet file."""


@dataclass(frozen=True)
class AlphabetInfo:
    """Metadata for selection lists, without the code table."""

    id: str
    name: str
    description: str
    path: Path
    kind: str = "alphabet"  # "alphabet" (JSON) or "telecode" (SQLite)
    user: bool = False      # created by the user, so editable and deletable


@dataclass(frozen=True)
class Alphabet:
    """A fully resolved alphabet, inherited codes included."""

    id: str
    name: str
    description: str
    codes: Mapping[str, str]  # read-only so nobody mutates the shared object
    combining: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))  # e.g. dakuten U+3099
    word_separator: str = " "  # typed on a word gap, "" for none (Japanese)
    abstract: bool = False     # only a base for ``extends``, not selectable

    def lookup(self, code: str) -> str | None:
        return self.codes.get(code)

    def encode(self, char: str) -> str | None:
        """Reverse lookup. Slow on purpose, there is no reverse table kept in memory."""
        for code, value in self.codes.items():
            if value == char:
                return code
        return None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json silently keeps the last of two equal keys, which hides data errors."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AlphabetError(f"doppelter Schlüssel {key!r}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh, object_pairs_hook=_reject_duplicate_keys)
    except AlphabetError as exc:
        raise AlphabetError(f"{path.name}: {exc}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise AlphabetError(f"{path.name}: nicht lesbar ({exc})") from exc
    if not isinstance(data, dict):
        raise AlphabetError(f"{path.name}: Wurzelelement muss ein Objekt sein")
    return data


def _validate_table(path: Path, table: Any, label: str) -> dict[str, str]:
    if not isinstance(table, dict):
        raise AlphabetError(f"{path.name}: '{label}' muss ein Objekt sein")
    for code, char in table.items():
        if not _CODE_PATTERN.match(code):
            raise AlphabetError(f"{path.name}: ungültiger Code {code!r} in '{label}' (nur '.' und '-')")
        if not isinstance(char, str) or not char:
            raise AlphabetError(f"{path.name}: Code {code!r} in '{label}' braucht ein nicht-leeres Zeichen")
    return dict(table)


def _read_info(path: Path) -> tuple[dict[str, Any], AlphabetInfo]:
    data = _read_json(path)
    alphabet_id = data.get("id")
    if not isinstance(alphabet_id, str) or not _ID_PATTERN.match(alphabet_id):
        raise AlphabetError(f"{path.name}: ungültige oder fehlende 'id'")
    # id must match the file name so extends="latin" finds latin.json without opening every file
    if alphabet_id != path.stem:
        raise AlphabetError(f"{path.name}: 'id' ({alphabet_id}) muss dem Dateinamen entsprechen")
    info = AlphabetInfo(
        id=alphabet_id,
        name=str(data.get("name") or alphabet_id),
        description=str(data.get("description") or ""),
        path=path,
    )
    return data, info


def load_alphabet(alphabet_id: str, directory: Path | Sequence[Path] = BUILTIN_ALPHABET_DIR) -> Alphabet:
    """Load ``<alphabet_id>.json`` and everything it extends.

    ``directory`` can be a list (search order) so user alphabets can extend
    built-in ones. The child wins over its parents: in wabun ".-.-.-" becomes
    the Japanese comma while ``common`` has the full stop.
    """
    directories = [Path(directory)] if isinstance(directory, (str, Path)) else [Path(d) for d in directory]
    chain: list[dict[str, Any]] = []
    seen: list[str] = []
    current: str | None = alphabet_id

    while current is not None:
        if not isinstance(current, str) or not _ID_PATTERN.match(current):
            raise AlphabetError(f"ungültige Alphabet-ID {current!r}")
        if current in seen:
            raise AlphabetError(f"zyklisches 'extends': {' → '.join([*seen, current])}")
        seen.append(current)
        candidates = [d / f"{current}.json" for d in directories]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            raise AlphabetError(f"Alphabet '{current}' nicht gefunden ({candidates[0]})")
        data, _ = _read_info(path)
        data["_path"] = path
        chain.append(data)
        current = data.get("extends")

    # merge root first so the child overrides
    codes: dict[str, str] = {}
    combining: dict[str, str] = {}
    for data in reversed(chain):
        path = data["_path"]
        codes.update(_validate_table(path, data.get("codes", {}), "codes"))
        combining.update(_validate_table(path, data.get("combining", {}), "combining"))

    leaf = chain[0]
    leaf_path = leaf["_path"]
    overlap = sorted(set(codes) & set(combining))
    if overlap:
        raise AlphabetError(f"{leaf_path.name}: Codes sowohl in 'codes' als auch 'combining': {overlap}")
    if not codes:
        raise AlphabetError(f"{leaf_path.name}: enthält keine Codes")

    separator = leaf.get("word_separator", " ")
    if not isinstance(separator, str):
        raise AlphabetError(f"{leaf_path.name}: 'word_separator' muss ein String sein")

    return Alphabet(
        id=leaf["id"],
        name=str(leaf.get("name") or leaf["id"]),
        description=str(leaf.get("description") or ""),
        codes=MappingProxyType(codes),
        combining=MappingProxyType(combining),
        word_separator=separator,
        abstract=bool(leaf.get("abstract", False)),
    )


class DictionaryManager:
    """Lists the available alphabets and keeps only the active one in memory.

    ``select`` runs on the UI thread, the decoder reads from the Rust worker
    thread, so switching is guarded by a lock.
    """

    TELECODE_ID = "chinese-telecode"  # virtual id, backed by SQLite

    def __init__(
        self,
        directory: Path | None = None,
        telecode_db: Path | None = None,
        user_directory: Path | None = None,
        smiley_directory: Path | None = None,
        user_smiley_directory: Path | None = None,
    ) -> None:
        self._directory = Path(directory) if directory else BUILTIN_ALPHABET_DIR
        self._smiley_directory = Path(smiley_directory) if smiley_directory else BUILTIN_SMILEY_DIR
        self._user_smiley_directory = Path(user_smiley_directory) if user_smiley_directory else None
        self._smiley_id = ""  # "" = off
        self._smileys: dict[str, str] = {}
        self._user_directory = Path(user_directory) if user_directory else None
        self._telecode_db = Path(telecode_db) if telecode_db else None
        self._lock = threading.Lock()
        self._active: Alphabet | None = None
        self.load_count = 0  # real disk loads, for tests

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def user_directory(self) -> Path | None:
        return self._user_directory

    @property
    def telecode_db(self) -> Path | None:
        return self._telecode_db

    @property
    def smiley_directory(self) -> Path:
        return self._smiley_directory

    @property
    def user_smiley_directory(self) -> Path | None:
        return self._user_smiley_directory

    def smiley_sets(self) -> list[SmileySetInfo]:
        return list_smiley_sets(self._smiley_directory, self._user_smiley_directory)

    def set_smileys(self, set_id: str) -> None:
        """Pick a smiley set ("" = off). On error the previous state stays."""
        codes = load_smiley_codes(set_id, self._smiley_directory, self._user_smiley_directory)
        with self._lock:
            self._smiley_id, self._smileys = set_id, codes
            self._active = None  # next select() rebuilds with the new smileys

    @property
    def smiley_set_id(self) -> str:
        return self._smiley_id

    def _search_dirs(self) -> list[Path]:
        # built-in first, so user files can't shadow them
        return [self._directory, *([self._user_directory] if self._user_directory else [])]

    def invalidate(self) -> None:
        """Forget the loaded alphabet, e.g. after a file changed on disk."""
        with self._lock:
            self._active = None

    def available(self) -> list[AlphabetInfo]:
        """All selectable alphabets sorted by name. Broken files are skipped."""
        found: dict[str, AlphabetInfo] = {}
        for directory in self._search_dirs():
            is_user = directory != self._directory
            for path in sorted(directory.glob("*.json")):
                try:
                    data, info = _read_info(path)
                except AlphabetError:
                    continue
                if data.get("abstract") or info.id in found:
                    continue  # abstract bases (common.json) and duplicate ids: first wins
                found[info.id] = dataclasses.replace(info, user=is_user)
        infos = list(found.values())
        if self._telecode_db is not None and self._telecode_db.is_file():
            infos.append(
                AlphabetInfo(
                    id=self.TELECODE_ID,
                    name="Chinesisch (Telegrafencode)",
                    description="4-stellige chinesische Telegrafencodes aus SQLite.",
                    path=self._telecode_db,
                    kind="telecode",
                )
            )
        return sorted(infos, key=lambda i: i.name.casefold())

    def load(self, alphabet_id: str) -> Alphabet:
        """Load without activating (the telecode decoder needs ``common`` for digits)."""
        alphabet = load_alphabet(alphabet_id, self._search_dirs())
        self.load_count += 1
        return alphabet

    def select(self, alphabet_id: str) -> Alphabet:
        """Make ``alphabet_id`` the active one, loading it lazily."""
        with self._lock:
            if self._active is not None and self._active.id == alphabet_id:
                return self._active
            if alphabet_id == self.TELECODE_ID:
                raise AlphabetError("Telecodes sind kein JSON-Alphabet – create_decoder() benutzen")
            alphabet = self.load(alphabet_id)
            if alphabet.abstract:
                raise AlphabetError(f"Alphabet '{alphabet_id}' ist abstrakt und nicht auswählbar")
            if self._smileys:
                # smileys sit under the alphabet, so a letter wins on the same code
                extra = {c: s for c, s in self._smileys.items() if c not in alphabet.combining}
                alphabet = dataclasses.replace(alphabet, codes=MappingProxyType({**extra, **alphabet.codes}))
            self._active = alphabet
            return alphabet

    @property
    def active(self) -> Alphabet | None:
        return self._active

    def lookup(self, code: str) -> str | None:
        alphabet = self._active
        return alphabet.lookup(code) if alphabet else None
