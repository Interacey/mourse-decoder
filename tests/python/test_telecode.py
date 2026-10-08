"""Tests for the SQLite telecode database and the build CLI."""

from __future__ import annotations

import sqlite3
import zipfile

import pytest
from conftest import UNIHAN_SAMPLE
from mourse_decoder.telecode import (
    TelecodeError,
    TelecodeStore,
    build_database,
    main,
    parse_unihan,
)


def test_parse_unihan_filters_field():
    pairs = list(parse_unihan(UNIHAN_SAMPLE.splitlines()))
    assert ("0022", "中") in pairs
    assert ("0948", "国") in pairs  # mainland (simplified)
    assert ("0948", "國") not in pairs  # the Taiwan entry belongs to a different field
    assert all(len(code) == 4 for code, _ in pairs)


def test_parse_unihan_taiwan_field():
    pairs = list(parse_unihan(UNIHAN_SAMPLE.splitlines(), field="kTaiwanTelegraph"))
    assert pairs == [("0948", "國")]


def test_lookup(telecode_db):
    with TelecodeStore(telecode_db) as store:
        assert store.lookup("0022") == "中"
        assert store.lookup("0086") == "人"
        assert store.lookup("1234") is None


@pytest.mark.parametrize("bad", ["", "22", "00220", "abcd", "0022' OR '1'='1"])
def test_lookup_rejects_malformed_codes(telecode_db, bad):
    # the SQL injection attempt never ends up in the query anyway (placeholder)
    with TelecodeStore(telecode_db) as store:
        assert store.lookup(bad) is None


def test_duplicates_first_wins(tmp_path):
    inserted, skipped = build_database([("9999", "一"), ("9999", "二")], tmp_path / "t.sqlite")
    assert (inserted, skipped) == (1, 1)
    with TelecodeStore(tmp_path / "t.sqlite") as store:
        assert store.lookup("9999") == "一"


def test_store_is_read_only(telecode_db):
    store = TelecodeStore(telecode_db)
    with pytest.raises(sqlite3.OperationalError):
        store._conn.execute("DELETE FROM telecode")  # noqa: SLF001
    store.close()


def test_missing_db_raises(tmp_path):
    with pytest.raises(TelecodeError, match="nicht gefunden"):
        TelecodeStore(tmp_path / "fehlt.sqlite")


def test_foreign_sqlite_file_raises(tmp_path):
    path = tmp_path / "anders.sqlite"
    sqlite3.connect(path).execute("CREATE TABLE x (y)").connection.close()
    with pytest.raises(TelecodeError, match="Ungültige"):
        TelecodeStore(path)


def test_invalid_entries_abort_build_without_leaving_files(tmp_path):
    target = tmp_path / "t.sqlite"
    with pytest.raises(TelecodeError):
        build_database([("0022", "中"), ("12", "x")], target)
    # atomic build: neither the target nor the temp file is left behind
    assert list(tmp_path.iterdir()) == []


def test_len(telecode_db):
    with TelecodeStore(telecode_db) as store:
        assert len(store) == 4  # 0022, 0948, 0086, 9999


def test_store_works_from_other_thread(telecode_db):
    # in the app, lookups come from the Rust worker thread
    import threading

    store = TelecodeStore(telecode_db)
    results = []
    thread = threading.Thread(target=lambda: results.append(store.lookup("0022")))
    thread.start()
    thread.join()
    store.close()
    assert results == ["中"]


def test_cli_from_text_file(tmp_path, capsys):
    source = tmp_path / "Unihan_OtherMappings.txt"
    source.write_text(UNIHAN_SAMPLE, encoding="utf-8")
    assert main([str(source), str(tmp_path / "out.sqlite")]) == 0
    assert "4 Codes geschrieben" in capsys.readouterr().out


def test_cli_from_zip(tmp_path):
    archive = tmp_path / "Unihan.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Unihan_OtherMappings.txt", UNIHAN_SAMPLE)
    assert main([str(archive), str(tmp_path / "out.sqlite")]) == 0
    with TelecodeStore(tmp_path / "out.sqlite") as store:
        assert store.lookup("0022") == "中"


def test_cli_zip_without_mapping_file(tmp_path, capsys):
    archive = tmp_path / "Unihan.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("andere.txt", "")
    assert main([str(archive), str(tmp_path / "out.sqlite")]) == 1
    assert "fehlt" in capsys.readouterr().err


def test_cli_with_no_entries_fails(tmp_path, capsys):
    source = tmp_path / "leer.txt"
    source.write_text("# nichts\n", encoding="utf-8")
    assert main([str(source), str(tmp_path / "out.sqlite")]) == 1
    assert not (tmp_path / "out.sqlite").exists()
