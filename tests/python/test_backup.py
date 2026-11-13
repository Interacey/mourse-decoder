"""Config backup: export, inspect and import of the zip bundle (pure logic, no display)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path

import pytest
from mourse_decoder import backup
from mourse_decoder.backup import BackupError, export_bundle, import_bundle, inspect_bundle
from mourse_decoder.function_keys import SCHEMA_VERSION, FunctionKeys, FunctionKeysStore
from mourse_decoder.settings import Settings, SettingsStore


def _real_telecode_db() -> bytes:
    """A database built by the program's own builder, so the import's structure check sees the real schema."""
    from mourse_decoder.telecode import build_database

    with tempfile.TemporaryDirectory() as folder:
        build_database([("0001", "一"), ("0002", "丁")], Path(folder) / "t.sqlite")
        return (Path(folder) / "t.sqlite").read_bytes()


SQLITE = _real_telecode_db()


def make_config(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text('{"alphabet": "de"}', encoding="utf-8")
    (root / "function_keys.json").write_text(json.dumps({"schema_version": 1, "slots": []}), encoding="utf-8")
    content = {  # files the program's own loaders accept
        "alphabets": {"codes": {".": "e"}},
        "smileys": {"codes": {".-": "x"}},
        "languages": {"strings": {}},
    }
    for folder, name in (("alphabets", "mein.json"), ("smileys", "set.json"), ("languages", "xx.json")):
        (root / folder).mkdir(exist_ok=True)
        data = {"id": name[:-5], "name": "Größe ✓", **content[folder]}
        (root / folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def build_zip(path: Path, files: dict[str, bytes], manifest: dict | None | bool = True, extra: dict[str, bytes] | None = None) -> Path:
    """Zip with a correct manifest for ``files`` (or a custom one); ``extra`` entries are added unlisted."""
    if manifest is True:
        manifest = {
            "format": backup.FORMAT_NAME,
            "format_version": backup.FORMAT_VERSION,
            "files": [{"path": n, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for n, d in files.items()],
        }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        if manifest:
            z.writestr("manifest.json", json.dumps(manifest))
        for name, data in {**files, **(extra or {})}.items():
            z.writestr(name, data)
    return path


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path / "cfg" / "MourseDecoder")


@pytest.fixture
def bundle(cfg, tmp_path):
    path = tmp_path / "b.zip"
    export_bundle(cfg, path)
    return path


# export


def test_manifest_fields_and_file_list(cfg, tmp_path):
    manifest = export_bundle(cfg, tmp_path / "b.zip")
    assert manifest["format"] == "mourse-decoder-backup"
    assert manifest["format_version"] == 1
    assert manifest["app_version"]
    assert manifest["created_utc"].endswith("Z")
    paths = [f["path"] for f in manifest["files"]]
    assert paths == ["settings.json", "function_keys.json", "alphabets/mein.json", "smileys/set.json", "languages/xx.json"]
    entry = manifest["files"][1]
    assert entry["schema_version"] == SCHEMA_VERSION
    assert entry["size"] == len((cfg / "function_keys.json").read_bytes())
    assert entry["sha256"] == hashlib.sha256((cfg / "function_keys.json").read_bytes()).hexdigest()
    with zipfile.ZipFile(tmp_path / "b.zip") as z:
        assert set(z.namelist()) == {"manifest.json", *paths}
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist())
        assert all(not i.filename.startswith("/") and ":" not in i.filename for i in z.infolist())


def test_round_trip_into_an_empty_folder_gives_the_same_bytes(cfg, bundle, tmp_path):
    target = tmp_path / "new" / "MourseDecoder"
    result = import_bundle(bundle, target)
    assert snapshot(target) == snapshot(cfg)
    assert len(result.written) == 5
    # the restore point of an import into an empty folder holds no files but lists the ones the import created
    assert sorted(p.name for p in (tmp_path / "new").iterdir()) == ["MourseDecoder", result.restore_point.name]
    import_bundle(result.restore_point, target)  # "undo": what the import created is gone again
    assert snapshot(target) == {}


def test_bak_and_temp_files_are_not_exported(cfg, tmp_path):
    (cfg / "settings.json.bak").write_text("{}")
    (cfg / "settings.json.bak2").write_text("{}")
    (cfg / "alphabets" / "mein.json.bak").write_text("{}")
    (cfg / "alphabets" / "x.json.tmp").write_text("{}")
    (cfg / "alphabets" / "notes.txt").write_text("hi")
    (cfg / "alphabets" / ".hidden.json").write_text("{}")
    manifest = export_bundle(cfg, tmp_path / "b.zip")
    assert [f["path"] for f in manifest["files"] if f["path"].startswith("alphabets/")] == ["alphabets/mein.json"]
    assert not any(".bak" in f["path"] or ".tmp" in f["path"] for f in manifest["files"])


def test_telecode_is_excluded_by_default_and_optional(cfg, tmp_path):
    (cfg / "telecode.sqlite").write_bytes(SQLITE)
    default = export_bundle(cfg, tmp_path / "a.zip")
    assert "telecode.sqlite" not in [f["path"] for f in default["files"]]
    assert default["contains_telecode"] is False
    full = export_bundle(cfg, tmp_path / "b.zip", include_telecode=True)
    assert "telecode.sqlite" in [f["path"] for f in full["files"]]
    target = tmp_path / "t"
    import_bundle(tmp_path / "b.zip", target)
    assert (target / "telecode.sqlite").read_bytes() == SQLITE


def test_export_with_nothing_to_back_up_fails(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(BackupError, match="nothing to back up"):
        export_bundle(tmp_path / "empty", tmp_path / "b.zip")
    assert not (tmp_path / "b.zip").exists()


def test_export_does_not_follow_symlinks_out_of_the_folder(cfg, tmp_path):
    outside = tmp_path / "secret.json"
    outside.write_text('{"x": 1}')
    outside_dir = tmp_path / "outdir"
    outside_dir.mkdir()
    (outside_dir / "evil.json").write_text("{}")
    try:
        os.symlink(outside, cfg / "alphabets" / "link.json")
        os.symlink(outside_dir, cfg / "smileys2", target_is_directory=True)
        (cfg / "languages").rename(cfg / "languages_real")
        os.symlink(outside_dir, cfg / "languages", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    manifest = export_bundle(cfg, tmp_path / "b.zip")
    paths = [f["path"] for f in manifest["files"]]
    assert "alphabets/link.json" not in paths
    assert not any(p.startswith("languages/") for p in paths)


# round trip with real data


def test_bundle_from_the_apps_own_default_files_imports(tmp_path):
    src = tmp_path / "src"
    SettingsStore(src).save(Settings())
    keys = FunctionKeys()
    keys = keys.replace_slot(keys.slot(5).replace(macro="Hallo {Enter}", trigger="Ctrl+Alt+F5"))
    FunctionKeysStore(src).save(keys)
    (src / "alphabets").mkdir()
    (src / "alphabets" / "ä-test.json").write_text('{"id": "ä-test", "name": "Test", "map": {}}', encoding="utf-8")
    export_bundle(src, tmp_path / "b.zip")

    dst = tmp_path / "dst"
    import_bundle(tmp_path / "b.zip", dst)
    assert snapshot(dst) == snapshot(src)
    assert SettingsStore(dst).load() == Settings()
    loaded, warnings = FunctionKeysStore(dst).load()
    assert warnings == []
    assert loaded.slot(5).macro == "Hallo {Enter}"


def test_hand_written_json_with_raw_line_breaks_and_bom_is_accepted(tmp_path):
    src = tmp_path / "src"
    (src / "alphabets").mkdir(parents=True)
    (src / "settings.json").write_bytes(b'\xef\xbb\xbf{"alphabet": "de"}')
    (src / "alphabets" / "h.json").write_text('{"id": "h", "text": "zwei\nZeilen"}', encoding="utf-8")
    export_bundle(src, tmp_path / "b.zip")
    import_bundle(tmp_path / "b.zip", tmp_path / "dst")
    assert snapshot(tmp_path / "dst") == snapshot(src)


def test_non_ascii_names_round_trip(cfg, tmp_path):
    (cfg / "alphabets" / "Größe-日本語.json").write_text('{"id": "x"}', encoding="utf-8")
    export_bundle(cfg, tmp_path / "b.zip")
    import_bundle(tmp_path / "b.zip", tmp_path / "dst")
    assert (tmp_path / "dst" / "alphabets" / "Größe-日本語.json").read_text(encoding="utf-8") == '{"id": "x"}'


# preview


def test_preview_lists_add_replace_unchanged(cfg, bundle, tmp_path):
    target = tmp_path / "t"
    make_config(target)
    (target / "settings.json").write_text('{"alphabet": "fr"}')  # differs
    (target / "smileys" / "set.json").unlink()  # missing
    (target / "alphabets" / "other.json").write_text("{}")  # not in the bundle
    before = snapshot(target)
    preview = inspect_bundle(bundle, target)
    status = {e.path: e.status for e in preview.entries}
    assert status == {
        "settings.json": "replace",
        "function_keys.json": "unchanged",
        "alphabets/mein.json": "unchanged",
        "smileys/set.json": "add",
        "languages/xx.json": "unchanged",
    }
    assert (preview.count("add"), preview.count("replace"), preview.count("unchanged")) == (1, 1, 3)
    assert preview.manifest["format_version"] == 1
    assert snapshot(target) == before
    assert not list(tmp_path.glob("t-backup-*"))


# import with restore point


def test_replace_creates_a_restore_point_and_keeps_other_files(cfg, bundle, tmp_path):
    target = tmp_path / "work" / "MourseDecoder"
    make_config(target)
    (target / "settings.json").write_text('{"alphabet": "fr"}')
    (target / "alphabets" / "other.json").write_text('{"keep": 1}')
    original = snapshot(target)
    result = import_bundle(bundle, target)
    assert result.written == ("settings.json",)
    assert (target / "settings.json").read_text() == '{"alphabet": "de"}'
    assert (target / "alphabets" / "other.json").read_text() == '{"keep": 1}'
    rp = result.restore_point
    assert rp is not None and rp.parent == target.parent and rp.name.startswith("MourseDecoder-backup-")
    # the restore point is a bundle again: importing it brings the original back
    import_bundle(rp, target)
    assert snapshot(target) == original


def test_second_import_in_the_same_second_gets_its_own_restore_point(bundle, tmp_path):
    target = tmp_path / "t"
    make_config(target)
    points = set()
    for text in ('{"a": 1}', '{"a": 2}'):
        (target / "settings.json").write_text(text)
        points.add(import_bundle(bundle, target).restore_point)
    assert len(points) == 2 and all(p.exists() for p in points)


def test_unsupported_mode_is_refused(bundle, tmp_path):
    with pytest.raises(BackupError, match="mode"):
        import_bundle(bundle, tmp_path / "t", mode="merge")


def test_all_or_nothing_when_the_third_write_fails(bundle, tmp_path, monkeypatch):
    target = tmp_path / "t"
    make_config(target)
    for p in target.rglob("*.json"):
        p.write_text('{"old": true}')
    original = snapshot(target)
    real = backup._write_file
    calls = []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(backup, "_write_file", flaky)
    with pytest.raises(BackupError, match="left as it was"):
        import_bundle(bundle, target)
    assert len(calls) == 3
    assert snapshot(target) == original


def test_a_failed_import_removes_folders_it_created(bundle, tmp_path, monkeypatch):
    target = tmp_path / "t"
    target.mkdir()
    real = backup._write_file

    def flaky(path, data):
        if path.parent.name == "smileys":
            raise OSError("boom")
        real(path, data)

    monkeypatch.setattr(backup, "_write_file", flaky)
    with pytest.raises(BackupError):
        import_bundle(bundle, target)
    assert snapshot(target) == {}
    assert not (target / "alphabets").exists()


def test_restore_point_failure_writes_nothing(bundle, tmp_path, monkeypatch):
    target = tmp_path / "t"
    make_config(target)
    (target / "settings.json").write_text("{}")
    original = snapshot(target)

    def fail(*_a, **_k):
        raise BackupError("no space")

    monkeypatch.setattr(backup, "_write_zip", fail)
    with pytest.raises(BackupError):
        import_bundle(bundle, target)
    assert snapshot(target) == original


# rejections: nothing may be written


def assert_rejected(zip_path: Path, tmp_path: Path, match: str | None = None) -> None:
    target = tmp_path / "victim"
    make_config(target)
    before = snapshot(target)
    before_tree = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(BackupError, match=match):
        import_bundle(zip_path, target)
    with pytest.raises(BackupError):
        inspect_bundle(zip_path, target)
    assert snapshot(target) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == before_tree  # no restore point either


GOOD = b'{"id": "x"}'


@pytest.mark.parametrize(
    "name",
    [
        "../x.json", "alphabets/../../x.json", "/abs.json", "/etc/passwd", "C:\\x.json", "C:/x.json", "alphabets\\..\\..\\x.json",
        "alphabets\\x.json", "alphabets/sub/x.json", "alphabets/x.txt", "other/x.json/", "other/x.json", "x.json", "alphabets/..",
        "alphabets/.json", "alphabets/CON.json", "alphabets/nul.json", "alphabets/a:b.json", "alphabets/bad .json",
        "settings.json.bak", "telecode.sqlite.bak", "alphabets/x\x00.json", "alphabets/" + "a" * 130 + ".json",
    ],
)
def test_unsafe_or_unknown_names_are_rejected(name, tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b"{}"}, extra={name: GOOD})
    assert_rejected(zip_path, tmp_path)


def test_unsafe_name_listed_in_the_manifest_is_rejected(tmp_path):
    manifest = {"format": backup.FORMAT_NAME, "format_version": 1,
                "files": [{"path": "../x.json", "size": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}]}
    zip_path = build_zip(tmp_path / "z.zip", {}, manifest=manifest, extra={"../x.json": b"{}"})
    assert_rejected(zip_path, tmp_path)


def test_symlink_entry_is_rejected(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b"{}"})
    with zipfile.ZipFile(zip_path, "a") as z:
        info = zipfile.ZipInfo("alphabets/link.json")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "../../settings.json")
    assert_rejected(zip_path, tmp_path, "symbolic link")


def test_duplicate_and_case_duplicate_entries_are_rejected(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b"{}"})
    with zipfile.ZipFile(zip_path, "a") as z, pytest.warns(UserWarning):
        z.writestr("settings.json", b'{"a": 1}')
    assert_rejected(zip_path, tmp_path, "twice")
    zip2 = build_zip(tmp_path / "z2.zip", {"alphabets/a.json": GOOD}, extra={"alphabets/A.json": GOOD})
    assert_rejected(zip2, tmp_path, "twice")


def test_hash_mismatch_is_rejected(tmp_path):
    manifest = {"format": backup.FORMAT_NAME, "format_version": 1,
                "files": [{"path": "settings.json", "size": 2, "sha256": "0" * 64}]}
    assert_rejected(build_zip(tmp_path / "z.zip", {"settings.json": b"{}"}, manifest=manifest), tmp_path, "checksum")


def test_size_mismatch_in_manifest_is_rejected(tmp_path):
    manifest = {"format": backup.FORMAT_NAME, "format_version": 1,
                "files": [{"path": "settings.json", "size": 5, "sha256": hashlib.sha256(b"{}").hexdigest()}]}
    assert_rejected(build_zip(tmp_path / "z.zip", {"settings.json": b"{}"}, manifest=manifest), tmp_path, "checksum")


def test_file_in_zip_but_not_in_manifest_is_rejected(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b"{}"}, extra={"alphabets/sneaky.json": GOOD})
    assert_rejected(zip_path, tmp_path, "does not list")


def test_file_in_manifest_but_not_in_zip_is_rejected(tmp_path):
    manifest = {"format": backup.FORMAT_NAME, "format_version": 1,
                "files": [{"path": "settings.json", "size": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}]}
    assert_rejected(build_zip(tmp_path / "z.zip", {}, manifest=manifest), tmp_path, "missing in the zip")


def test_newer_schema_version_is_rejected(tmp_path):
    data = json.dumps({"schema_version": SCHEMA_VERSION + 1, "slots": []}).encode()
    assert_rejected(build_zip(tmp_path / "z.zip", {"function_keys.json": data}), tmp_path, "newer program version")


@pytest.mark.parametrize("version", ["1", 0, -1, True, 1.5, None])
def test_unusable_schema_version_is_rejected(tmp_path, version):
    data = json.dumps({"schema_version": version, "slots": []}).encode()
    assert_rejected(build_zip(tmp_path / "z.zip", {"function_keys.json": data}), tmp_path)


def test_function_keys_without_schema_version_is_accepted(tmp_path):
    target = tmp_path / "t"
    zip_path = build_zip(tmp_path / "z.zip", {"function_keys.json": b'{"slots": []}'})
    import_bundle(zip_path, target)
    assert (target / "function_keys.json").exists()


@pytest.mark.parametrize("data", [b"{not json", b"", b"[1, 2]", b"\xff\xfe\x00", b'"text"'])
def test_broken_json_is_rejected(tmp_path, data):
    assert_rejected(build_zip(tmp_path / "z.zip", {"alphabets/a.json": data}), tmp_path, "not usable")


def test_telecode_that_is_not_sqlite_is_rejected(tmp_path):
    assert_rejected(build_zip(tmp_path / "z.zip", {"telecode.sqlite": b"nope"}), tmp_path, "SQLite")


def test_manifest_missing_is_rejected(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b"{}"}, manifest=False)
    assert_rejected(zip_path, tmp_path, "manifest.json is missing")


@pytest.mark.parametrize("version", [2, 0, "1", None, True])
def test_unknown_format_version_is_rejected(tmp_path, version):
    manifest = {"format": backup.FORMAT_NAME, "format_version": version, "files": []}
    assert_rejected(build_zip(tmp_path / "z.zip", {}, manifest=manifest), tmp_path, "format version")


def test_wrong_format_name_and_broken_manifest_are_rejected(tmp_path):
    assert_rejected(build_zip(tmp_path / "a.zip", {}, manifest={"format": "other", "format_version": 1, "files": []}), tmp_path)
    broken = tmp_path / "b.zip"
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("manifest.json", "{nope")
    assert_rejected(broken, tmp_path, "not valid JSON")


def test_empty_bundle_and_non_zip_are_rejected(tmp_path):
    manifest = {"format": backup.FORMAT_NAME, "format_version": 1, "files": []}
    assert_rejected(build_zip(tmp_path / "z.zip", {}, manifest=manifest), tmp_path, "no files")
    (tmp_path / "x.zip").write_bytes(b"this is not a zip")
    assert_rejected(tmp_path / "x.zip", tmp_path, "not a readable zip")
    with pytest.raises(BackupError):
        inspect_bundle(tmp_path / "missing.zip", tmp_path)


def test_encrypted_entry_is_rejected(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b"{}"})
    raw = bytearray(zip_path.read_bytes())
    # set the "encrypted" bit in every local header and central directory entry
    for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        pos = raw.find(sig)
        while pos != -1:
            raw[pos + offset] |= 1
            pos = raw.find(sig, pos + 1)
    zip_path.write_bytes(bytes(raw))
    assert_rejected(zip_path, tmp_path, "encrypted")


def test_too_many_entries_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "MAX_ENTRIES", 3)
    files = {f"alphabets/a{i}.json": GOOD for i in range(4)}
    assert_rejected(build_zip(tmp_path / "z.zip", files), tmp_path, "more than 3 entries")


def test_entry_larger_than_the_limit_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "MAX_ENTRY_BYTES", 100)
    big = b'{"x": "' + b"a" * 200 + b'"}'
    assert_rejected(build_zip(tmp_path / "z.zip", {"alphabets/a.json": big}), tmp_path, "larger than")


def test_total_size_limit_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "MAX_TOTAL_BYTES", 150)
    files = {"alphabets/a.json": b'{"x": "' + os.urandom(20).hex().encode() + b'"}',
             "alphabets/b.json": b'{"x": "' + os.urandom(20).hex().encode() + b'"}',
             "alphabets/c.json": b'{"x": "' + os.urandom(20).hex().encode() + b'"}'}
    assert_rejected(build_zip(tmp_path / "z.zip", files), tmp_path, "when unpacked")


def test_zip_bomb_ratio_is_rejected_before_unpacking(tmp_path):
    bomb = b"{" + b" " * (3 * 512 * 1024) + b"}"  # 1.5 MiB of blanks (under the 2 MiB file limit) compress to a few KB
    zip_path = build_zip(tmp_path / "z.zip", {"alphabets/a.json": bomb})
    assert zip_path.stat().st_size < 30_000
    assert_rejected(zip_path, tmp_path, "zip bomb")


def test_declared_size_that_lies_is_rejected(tmp_path):
    zip_path = build_zip(tmp_path / "z.zip", {"settings.json": b'{"a": "' + b"x" * 40 + b'"}'})
    raw = bytearray(zip_path.read_bytes())
    # shrink the uncompressed size in the central directory entry of settings.json
    pos = raw.rfind(b"PK\x01\x02")
    pos = raw.find(b"PK\x01\x02", raw.find(b"PK\x01\x02") + 1) if raw.count(b"PK\x01\x02") > 1 else pos
    raw[pos + 24 : pos + 28] = (5).to_bytes(4, "little")
    zip_path.write_bytes(bytes(raw))
    assert_rejected(zip_path, tmp_path)


def test_target_that_is_not_a_plain_file_is_refused(bundle, tmp_path):
    target = tmp_path / "t"
    make_config(target)
    (target / "settings.json").unlink()
    (target / "settings.json").mkdir()
    with pytest.raises(BackupError, match="not a plain file"):
        import_bundle(bundle, target)


def test_a_linked_folder_in_the_target_is_refused(bundle, tmp_path):
    target = tmp_path / "t"
    elsewhere = tmp_path / "else"
    elsewhere.mkdir()
    target.mkdir()
    try:
        os.symlink(elsewhere, target / "alphabets", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    with pytest.raises(BackupError, match="link"):
        import_bundle(bundle, target)
    assert list(elsewhere.iterdir()) == []


def test_unchanged_import_writes_nothing_and_makes_no_restore_point(cfg, bundle, tmp_path):
    result = import_bundle(bundle, cfg)
    assert result.restore_point is None and result.written == ()
    assert len(result.unchanged) == 5
    assert not list(cfg.parent.glob("*backup*"))
