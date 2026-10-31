"""Update from a GitHub release and complete uninstall."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest
from mourse_decoder import __version__, updater
from mourse_decoder.updater import Release, UpdateError

GH = "https://github.com/Interacey/mourse-decoder/releases/download/v9.9.9/"
EXE = b"MZ" + b"\x00" * 1000
SHA = hashlib.sha256(EXE).hexdigest()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def release_json(tag="v9.9.9", assets=None, body="Neu: alles besser"):
    names = assets if assets is not None else ["MourseDecoder.exe", "MourseDecoder.exe.sha256"]
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/Interacey/mourse-decoder/releases/tag/{tag}",
        "body": body,
        "assets": [{"name": n, "browser_download_url": f"https://github.com/Interacey/mourse-decoder/releases/download/{tag}/{n}"} for n in names],
    }


def serving(routes: dict[str, bytes]):
    """urlopen stand-in: the URL's last part decides what comes back."""

    def urlopen(request, timeout=None):
        name = request.full_url.rsplit("/", 1)[-1]
        if name not in routes:
            raise urllib.error.URLError("nicht erreichbar")
        return FakeResponse(routes[name])

    return urlopen


# --- versions ---


@pytest.mark.parametrize(
    ("tag", "current", "newer"),
    [
        ("v0.1.3", "0.1.2", True),
        ("v.0.1.3", "0.1.2", True),  # the real tags look like this: v.0.1.1
        ("v.0.1.1", "0.1.2", False),
        ("0.2.0", "0.1.9", True),
        ("v1.0", "0.9.9", True),
        ("v0.1.2", "0.1.2", False),
        ("v0.1.1", "0.1.2", False),
        ("v0.1", "0.1.0", False),  # missing parts count as 0
        ("v0.10.0", "0.9.0", True),  # numbers, not text: 10 > 9
        ("v0.1.2-beta", "0.1.2", False),
    ],
)
def test_is_newer(tag, current, newer):
    assert updater.is_newer(tag, current) is newer


@pytest.mark.parametrize("bad", ["", "latest", "abc1"])
def test_parse_version_rejects_text_without_a_version(bad):
    with pytest.raises(ValueError):
        updater.parse_version(bad)


def test_this_is_version_0_1_2():
    assert __version__ == "0.1.2"


# --- looking at the latest release ---


def test_fetch_latest_reads_the_release():
    release = updater.fetch_latest(serving({"latest": json.dumps(release_json()).encode()}))
    assert release.tag == "v9.9.9" and release.notes == "Neu: alles besser"
    assert release.exe_url.endswith("/MourseDecoder.exe") and release.sha_url.endswith("/MourseDecoder.exe.sha256")
    assert release.page.startswith("https://github.com/")


def test_release_without_notes_or_checksum_still_loads():
    data = release_json(assets=["MourseDecoder.exe"], body=None)
    release = updater.fetch_latest(serving({"latest": json.dumps(data).encode()}))
    assert release.notes == "" and release.sha_url is None


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        ({}, "Keine Verbindung"),
        ({"latest": b"<html>kaputt"}, "Keine Verbindung"),
        ({"latest": b"[]"}, "Unerwartete"),
        ({"latest": json.dumps(release_json(tag="kein-tag")).encode()}, "Unerwartete"),
        ({"latest": json.dumps(release_json(assets=["Anderes.zip"])).encode()}, "keine MourseDecoder.exe"),
    ],
)
def test_fetch_latest_reports_problems(routes, message):
    with pytest.raises(UpdateError, match=message):
        updater.fetch_latest(serving(routes))


# --- download with checksum ---


def release(sha_url=GH + "MourseDecoder.exe.sha256"):
    return Release("v9.9.9", "", GH + "MourseDecoder.exe", sha_url, "https://github.com/x")


def test_download_checks_the_checksum(tmp_path):
    routes = {"MourseDecoder.exe": EXE, "MourseDecoder.exe.sha256": f"{SHA.upper()}  MourseDecoder.exe\n".encode()}
    path = updater.download(release(), tmp_path, serving(routes))
    assert path.read_bytes() == EXE


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        ({"MourseDecoder.exe": EXE + b"x", "MourseDecoder.exe.sha256": SHA.encode()}, "passt nicht"),
        ({"MourseDecoder.exe": EXE, "MourseDecoder.exe.sha256": b"keine summe"}, "ungültig"),
        ({"MourseDecoder.exe": EXE, "MourseDecoder.exe.sha256": b""}, "ungültig"),
        ({"MourseDecoder.exe.sha256": SHA.encode()}, "Keine Verbindung"),
        ({"MourseDecoder.exe": EXE}, "Keine Verbindung"),
    ],
)
def test_bad_download_leaves_nothing_behind(tmp_path, routes, message):
    with pytest.raises(UpdateError, match=message):
        updater.download(release(), tmp_path, serving(routes))
    assert list(tmp_path.iterdir()) == []


def test_release_without_checksum_is_refused(tmp_path):
    with pytest.raises(UpdateError, match="Prüfsumme"):
        updater.download(release(sha_url=None), tmp_path, serving({"MourseDecoder.exe": EXE}))


class Redirected(FakeResponse):
    """A response that ended up on another address, as after an HTTP redirect."""

    def __init__(self, data, final_url):
        super().__init__(data)
        self._final = final_url

    def geturl(self):
        return self._final


def test_a_redirect_to_plain_http_is_refused():
    def urlopen(request, timeout=None):
        return Redirected(b"{}", "http://evil.example/latest")

    with pytest.raises(UpdateError, match="unverschlüsselte"):
        updater.fetch_latest(urlopen)


def test_open_refuses_plain_http_itself():
    # second line of defence: whatever link a caller passes in, http is never opened
    with pytest.raises(UpdateError, match="Unsicher"):
        updater._open("http://example.org/x", serving({}))


def test_a_download_from_another_host_is_refused(tmp_path):
    foreign = Release("v9.9.9", "", "https://evil.example/MourseDecoder.exe", GH + "x.sha256", "https://github.com/x")
    with pytest.raises(UpdateError, match="nicht von GitHub"):
        updater.download(foreign, tmp_path, serving({"x.sha256": SHA.encode()}))


def test_a_release_page_outside_github_is_refused():
    data = release_json()
    data["html_url"] = "https://evil.example/phishing"
    with pytest.raises(UpdateError, match="Unerwartete"):
        updater.fetch_latest(serving({"latest": json.dumps(data).encode()}))


def test_an_oversized_release_answer_is_refused():
    huge = json.dumps(release_json(body="x" * (updater.MAX_JSON_BYTES + 10))).encode()
    with pytest.raises(UpdateError, match="zu groß"):
        updater.fetch_latest(serving({"latest": huge}))


def test_an_oversized_download_is_cut_off_and_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "MAX_EXE_BYTES", 500)
    routes = {"MourseDecoder.exe": EXE, "MourseDecoder.exe.sha256": SHA.encode()}
    with pytest.raises(UpdateError, match="größer als erwartet"):
        updater.download(release(), tmp_path, serving(routes))
    assert list(tmp_path.iterdir()) == []


def test_a_download_that_is_no_windows_program_is_refused(tmp_path):
    text = b"#!/bin/sh\necho hallo\n"
    routes = {"MourseDecoder.exe": text, "MourseDecoder.exe.sha256": hashlib.sha256(text).hexdigest().encode()}
    with pytest.raises(UpdateError, match="kein Windows-Programm"):
        updater.download(release(), tmp_path, serving(routes))
    assert list(tmp_path.iterdir()) == []


def test_plain_http_links_are_refused(tmp_path):
    insecure = Release("v9.9.9", "", "http://example.org/MourseDecoder.exe", "http://example.org/x.sha256", "https://x")
    with pytest.raises(UpdateError, match="nicht von GitHub"):
        updater.download(insecure, tmp_path, serving({}))


# --- scripts ---


def decoded(call_args) -> str:
    encoded = call_args[call_args.index("-EncodedCommand") + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


def test_script_runs_hidden_and_detached_with_a_clean_environment(monkeypatch):
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "C:\\alt")
    monkeypatch.setenv("_MEIPASS2", "C:\\alt")
    calls = []
    updater.run_after_exit("Write-Output 'ü'", lambda *a, **kw: calls.append((a, kw)))
    (args, kwargs), = calls
    assert decoded(args[0]) == "Write-Output 'ü'"
    assert kwargs["creationflags"] == 0x08000200 and kwargs["close_fds"] is True
    assert "_PYI_APPLICATION_HOME_DIR" not in kwargs["env"] and "_MEIPASS2" not in kwargs["env"]
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "-ExecutionPolicy" not in args[0] and "Bypass" not in args[0]  # the policy is left as the user set it


def test_quotes_in_paths_cannot_break_out_of_the_script():
    script = updater.update_script(Path("C:/it's/new.exe"), Path("C:/a'; Remove-Item x; '/app.exe"))
    assert "'C:\\it''s\\new.exe'" in script.replace("/", "\\") or "'C:/it''s/new.exe'" in script
    assert "a''; Remove-Item x; ''" in script  # stays inside the string


def powershell(script: str) -> int:
    """Run like the app does (hidden PowerShell with the encoded script) and return its exit code."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], timeout=60
    ).returncode


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_update_script_replaces_the_exe(tmp_path):
    new, target = tmp_path / "neu ä'x.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    assert powershell(updater.update_script(new, target, start=False, delay_s=0)) == 0
    assert target.read_bytes() == b"NEU" and not new.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_update_script_gives_up_with_exit_code_1_while_the_exe_stays_busy(tmp_path):
    new, target = tmp_path / "neu.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    with target.open("rb"):  # open files can't be overwritten, like a running exe
        assert powershell(updater.update_script(new, target, start=False, delay_s=0, tries=2)) == 1
    assert target.read_bytes() == b"ALT" and new.read_bytes() == b"NEU"  # nothing half-done


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_update_script_waits_while_the_exe_is_running(tmp_path):
    new, target = tmp_path / "neu.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    encoded = base64.b64encode(updater.update_script(new, target, start=False, delay_s=0).encode("utf-16-le"))
    with target.open("rb"):
        runner = subprocess.Popen(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded.decode()])
        with pytest.raises(subprocess.TimeoutExpired):
            runner.wait(timeout=2)
        assert target.read_bytes() == b"ALT"
    assert runner.wait(timeout=30) == 0
    assert target.read_bytes() == b"NEU"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_update_script_starts_the_new_program_and_reports_a_failed_start(tmp_path):
    new, target = tmp_path / "neu.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"kein Programm")  # replaced fine, but Windows cannot run it
    target.write_bytes(b"ALT")
    assert powershell(updater.update_script(new, target, delay_s=0)) == 2
    assert target.read_bytes() == b"kein Programm"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_uninstall_script_deletes_exe_and_folders(tmp_path):
    exe, config, legacy = tmp_path / "MourseDecoder.exe", tmp_path / "MourseDecoder", tmp_path / "PolyglotMorse"
    exe.write_bytes(b"x")
    for folder in (config, legacy):
        (folder / "alphabets").mkdir(parents=True)
        (folder / "settings.json").write_text("{}")
        (folder / "alphabets" / "mein.json").write_text("{}")
    untouched = tmp_path / "Dokumente"
    untouched.mkdir()
    assert powershell(updater.uninstall_script(exe, [config, legacy], delay_s=0)) == 0
    assert not exe.exists() and not config.exists() and not legacy.exists()
    assert untouched.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_uninstall_script_without_exe_still_removes_the_folders(tmp_path):
    config = tmp_path / "MourseDecoder"
    config.mkdir()
    assert powershell(updater.uninstall_script(None, [config, tmp_path / "gibt-es-nicht"], delay_s=0)) == 0
    assert not config.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_uninstall_script_reports_what_it_could_not_delete(tmp_path):
    exe, config = tmp_path / "MourseDecoder.exe", tmp_path / "MourseDecoder"
    exe.write_bytes(b"x")
    config.mkdir()
    with exe.open("rb"):  # still running
        assert powershell(updater.uninstall_script(exe, [config], delay_s=0, tries=2)) == 1
    assert exe.exists() and not config.exists()  # the settings are gone anyway

# --- install and uninstall as the app calls them ---


@pytest.mark.parametrize("name", ["", "Documents", "Windows", "System32", "MourseDecoder2", "mourse"])
def test_uninstall_only_ever_deletes_folders_with_the_programs_names(tmp_path, name):
    # a wrong APPDATA must not point the recursive delete at anything else
    folder = tmp_path / name
    with pytest.raises(UpdateError, match="gehört nicht zum Programm"):
        updater.uninstall_targets(folder)
    with pytest.raises(UpdateError):
        updater.uninstall(folder, lambda *a, **kw: pytest.fail("darf nichts starten"))


def test_uninstall_refuses_a_drive_root():
    with pytest.raises(UpdateError):
        updater.uninstall_targets(Path("C:/"))


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_update_script_removes_its_own_download_folder_only(tmp_path):
    own = tmp_path / (updater.TEMP_PREFIX + "abc")
    own.mkdir()
    new, target = own / "MourseDecoder.exe", tmp_path / "App.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    assert powershell(updater.update_script(new, target, start=False, delay_s=0)) == 0
    assert target.read_bytes() == b"NEU" and not own.exists()
    # a folder that is not ours (here: the temp dir of this test) stays
    other = tmp_path / "Eigene Dateien"
    other.mkdir()
    new2 = other / "neu.exe"
    new2.write_bytes(b"NEU2")
    assert powershell(updater.update_script(new2, target, start=False, delay_s=0)) == 0
    assert other.exists()


def test_failed_download_leaves_no_temp_folder(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    made = []
    real_mkdtemp = updater.tempfile.mkdtemp
    monkeypatch.setattr(updater.tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])
    with pytest.raises(UpdateError):
        updater.install(release(), serving({}), lambda *a, **kw: pytest.fail("darf nichts starten"))
    assert made and not Path(made[0]).exists()


def test_uninstall_targets_include_the_old_folder(tmp_path):
    assert updater.uninstall_targets(tmp_path / "MourseDecoder") == [tmp_path / "MourseDecoder", tmp_path / "PolyglotMorse"]
    assert updater.uninstall_targets(tmp_path / "mourse-decoder") == [tmp_path / "mourse-decoder"]


@pytest.mark.skipif(sys.platform != "win32", reason="nur Windows nutzt das Skript")
def test_uninstall_from_a_dev_checkout_removes_only_the_folders(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    calls = []
    updater.uninstall(tmp_path / "MourseDecoder", lambda *a, **kw: calls.append(a))
    script = decoded(calls[0][0])
    assert "MourseDecoder'" in script and "$exe" not in script  # python.exe is never deleted


@pytest.mark.skipif(sys.platform != "win32", reason="nur Windows nutzt das Skript")
def test_uninstall_from_the_exe_also_removes_the_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    calls = []
    updater.uninstall(tmp_path / "MourseDecoder", lambda *a, **kw: calls.append(a))
    assert sys.executable.replace("'", "''") in decoded(calls[0][0])


@pytest.mark.skipif(sys.platform == "win32", reason="Nicht-Windows löscht direkt")
def test_uninstall_elsewhere_deletes_directly(tmp_path):
    config = tmp_path / "mourse-decoder"
    (config / "alphabets").mkdir(parents=True)
    updater.uninstall(config)
    assert not config.exists()


def test_install_refuses_outside_the_exe(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    with pytest.raises(UpdateError, match="MourseDecoder.exe"):
        updater.install(release(), serving({}), lambda *a, **kw: pytest.fail("darf nichts starten"))


@pytest.mark.skipif(sys.platform != "win32", reason="nur Windows kann sich selbst ersetzen")
def test_install_downloads_checks_and_schedules_the_swap(tmp_path, monkeypatch):
    fake_exe = tmp_path / "MourseDecoder.exe"
    fake_exe.write_bytes(b"ALT")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    calls = []
    routes = {"MourseDecoder.exe": EXE, "MourseDecoder.exe.sha256": SHA.encode()}
    updater.install(release(), serving(routes), lambda *a, **kw: calls.append(a))
    script = decoded(calls[0][0])
    assert str(fake_exe) in script and "Start-Process" in script
    assert "mourse-update-" in script  # the verified download is what gets moved


@pytest.mark.skipif(sys.platform != "win32", reason="nur Windows kann sich selbst ersetzen")
def test_a_bad_checksum_never_starts_the_swap(tmp_path, monkeypatch):
    fake_exe = tmp_path / "MourseDecoder.exe"
    fake_exe.write_bytes(b"ALT")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    routes = {"MourseDecoder.exe": EXE, "MourseDecoder.exe.sha256": b"0" * 64}
    with pytest.raises(UpdateError, match="passt nicht"):
        updater.install(release(), serving(routes), lambda *a, **kw: pytest.fail("darf nichts starten"))
    assert fake_exe.read_bytes() == b"ALT"
