"""Chinese telegraph codes in SQLite.

The mapping has about 10,000 entries, so it lives in SQLite instead of a dict in
memory. The data is built from the official Unihan database (kMainlandTelegraph)
rather than shipped with the app. Japanese kanji telegraph codes aren't in
Unihan and aren't supported.
"""

from __future__ import annotations

import argparse
import io
import re
import sqlite3
import sys
import threading
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TextIO

# e.g. "U+4E2D<TAB>kMainlandTelegraph<TAB>0022"
_UNIHAN_LINE = re.compile(r"^U\+([0-9A-F]{4,6})\t(kMainlandTelegraph|kTaiwanTelegraph)\t(\d{4})\s*$")
_CODE_PATTERN = re.compile(r"^\d{4}$")

UNIHAN_FILE = "Unihan_OtherMappings.txt"
SCHEMA = """
CREATE TABLE IF NOT EXISTS telecode (
    code TEXT PRIMARY KEY,   -- 4 digits, e.g. '0022'
    char TEXT NOT NULL       -- exactly one hanzi
) WITHOUT ROWID;
"""


class TelecodeError(RuntimeError):
    """Database missing or broken, or invalid input data."""


class TelecodeStore:
    """Read-only lookup in the telecode database."""

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        if not db_path.is_file():
            raise TelecodeError(f"Telecode-Datenbank nicht gefunden: {db_path}")
        # read-only; lookups come from the Rust worker thread, so share the
        # connection across threads and guard it with our own lock
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        try:
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.execute("SELECT 1 FROM telecode LIMIT 1")
        except sqlite3.Error as exc:
            raise TelecodeError(f"Ungültige Telecode-Datenbank {db_path}: {exc}") from exc
        self._lock = threading.Lock()
        self.path = db_path

    def lookup(self, code: str) -> str | None:
        if not _CODE_PATTERN.match(code):
            return None
        with self._lock:
            row = self._conn.execute("SELECT char FROM telecode WHERE code = ?", (code,)).fetchone()
        return row[0] if row else None

    def __len__(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM telecode").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> TelecodeStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def parse_unihan(lines: Iterable[str], field: str = "kMainlandTelegraph") -> Iterator[tuple[str, str]]:
    """Yield (code, char) for every line of the wanted field."""
    for line in lines:
        match = _UNIHAN_LINE.match(line)
        if match and match.group(2) == field:
            yield match.group(3), chr(int(match.group(1), 16))


def _open_unihan(source: Path) -> TextIO:
    """Accepts the text file directly or the official Unihan.zip."""
    if source.suffix.lower() == ".zip":
        archive = zipfile.ZipFile(source)
        try:
            raw = archive.open(UNIHAN_FILE)
        except KeyError as exc:
            archive.close()
            raise TelecodeError(f"{UNIHAN_FILE} fehlt in {source}") from exc
        return io.TextIOWrapper(raw, encoding="utf-8")
    return source.open(encoding="utf-8")


def build_database(
    pairs: Iterable[tuple[str, str]],
    db_path: Path,
) -> tuple[int, int]:
    """Write the mapping to a new SQLite file; returns (inserted, skipped).

    Duplicate codes are skipped, the first one wins. It writes to a temp file
    and renames it, so an aborted build never leaves a half-written database.
    """
    db_path = Path(db_path)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)
    inserted = skipped = 0
    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(SCHEMA)
        with conn:  # one transaction, much faster than autocommit
            for code, char in pairs:
                if not _CODE_PATTERN.match(code) or len(char) != 1:
                    raise TelecodeError(f"ungültiger Eintrag {code!r} → {char!r}")
                cursor = conn.execute("INSERT OR IGNORE INTO telecode(code, char) VALUES (?, ?)", (code, char))
                if cursor.rowcount:
                    inserted += 1
                else:
                    skipped += 1
        conn.execute("VACUUM")
    except BaseException:
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise
    conn.close()
    tmp_path.replace(db_path)
    return inserted, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Erzeugt telecode.sqlite aus der Unicode-Unihan-Datenbank.",
        epilog="Unihan.zip: https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip",
    )
    parser.add_argument("source", type=Path, help="Unihan.zip oder Unihan_OtherMappings.txt")
    parser.add_argument("output", type=Path, help="Ziel, z. B. telecode.sqlite")
    parser.add_argument(
        "--field",
        default="kMainlandTelegraph",
        choices=["kMainlandTelegraph", "kTaiwanTelegraph"],
        help="Festland- (Standard) oder Taiwan-Codes",
    )
    args = parser.parse_args(argv)
    try:
        with _open_unihan(args.source) as fh:
            inserted, skipped = build_database(parse_unihan(fh, args.field), args.output)
    except (OSError, TelecodeError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    if inserted == 0:
        print("Fehler: keine Einträge gefunden – richtige Datei?", file=sys.stderr)
        args.output.unlink(missing_ok=True)
        return 1
    print(f"{inserted} Codes geschrieben ({skipped} Duplikate übersprungen) → {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
