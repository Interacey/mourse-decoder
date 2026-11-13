"""Update from a GitHub release and complete uninstall."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import io
import json
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest
from mourse_decoder import __version__, signing, updater
from mourse_decoder.i18n import set_language
from mourse_decoder.updater import Release, UpdateError

GH = "https://github.com/Interacey/mourse-decoder/releases/download/v9.9.9/"
EXE = b"MZ" + b"\x00" * 1000
SHA = hashlib.sha256(EXE).hexdigest()
SEED = signing.generate_seed()  # the test release key; the real one is pinned in signing.py
KEY = signing.public_key(SEED)


def signature(tag="v9.9.9", name="MourseDecoder.exe", sha=SHA, seed=SEED) -> bytes:
    """The text of a MourseDecoder.exe.sig file."""
    return signing.encode_signature(signing.sign(seed, signing.build_manifest(tag, name, sha))).encode()


SIG = signature()
ROUTES = {"MourseDecoder.exe": EXE, "MourseDecoder.exe.sha256": SHA.encode(), "MourseDecoder.exe.sig": SIG}


@pytest.fixture(autouse=True)
def english():
    set_language("en")  # the error texts come from the language files
    yield
    set_language("en")


@pytest.fixture(autouse=True)
def pinned_key(monkeypatch):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (KEY,))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def release_json(tag="v9.9.9", assets=None, body="Neu: alles besser"):
    names = assets if assets is not None else ["MourseDecoder.exe", "MourseDecoder.exe.sha256", "MourseDecoder.exe.sig"]
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
        ("v0.1.5", "0.1.4", True),
        ("v.0.1.5", "0.1.4", True),  # the real tags look like this: v.0.1.2
        ("v.0.1.3", "0.1.4", False),
        ("0.2.0", "0.1.9", True),
        ("v1.0", "0.9.9", True),
        ("v0.1.4", "0.1.4", False),
        ("v0.1.3", "0.1.4", False),
        ("v0.1", "0.1.0", False),  # missing parts count as 0
        ("v0.10.0", "0.9.0", True),  # numbers, not text: 10 > 9
        ("v0.1.4-beta", "0.1.4", False),
    ],
)
def test_is_newer(tag, current, newer):
    assert updater.is_newer(tag, current) is newer


@pytest.mark.parametrize("bad", ["", "latest", "abc1"])
def test_parse_version_rejects_text_without_a_version(bad):
    with pytest.raises(ValueError):
        updater.parse_version(bad)


def test_parse_version_ignores_case_of_the_v():
    assert updater.parse_version("V.1.2") == (1, 2)


def test_this_is_version_0_1_9():
    assert __version__ == "0.1.9"


# --- looking at the latest release ---


def test_fetch_latest_reads_the_release():
    release = updater.fetch_latest(serving({"latest": json.dumps(release_json()).encode()}))
    assert release.tag == "v9.9.9" and release.notes == "Neu: alles besser"
    assert release.exe_url.endswith("/MourseDecoder.exe") and release.sha_url.endswith("/MourseDecoder.exe.sha256")
    assert release.sig_url.endswith("/v9.9.9/MourseDecoder.exe.sig")
    assert release.page.startswith("https://github.com/")


def test_release_without_notes_checksum_or_signature_still_loads():
    data = release_json(assets=["MourseDecoder.exe"], body=None)
    release = updater.fetch_latest(serving({"latest": json.dumps(data).encode()}))
    assert release.notes == "" and release.sha_url is None and release.sig_url is None


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        ({}, "No connection to GitHub"),
        ({"latest": b"<html>kaputt"}, "No connection to GitHub"),
        ({"latest": b"[]"}, "Unerwartete"),
        ({"latest": json.dumps(release_json(tag="kein-tag")).encode()}, "Unerwartete"),
        ({"latest": json.dumps(release_json(assets=["Anderes.zip"])).encode()}, "contains no MourseDecoder.exe"),
    ],
)
def test_fetch_latest_reports_problems(routes, message):
    with pytest.raises(UpdateError, match=message):
        updater.fetch_latest(serving(routes))


# --- download with checksum ---


def release(sha_url=GH + "MourseDecoder.exe.sha256", sig_url=GH + "MourseDecoder.exe.sig"):
    return Release("v9.9.9", "", GH + "MourseDecoder.exe", sha_url, "https://github.com/x", sig_url)


def test_download_checks_the_checksum(tmp_path):
    routes = {**ROUTES, "MourseDecoder.exe.sha256": f"{SHA.upper()}  MourseDecoder.exe\n".encode()}
    path = updater.download(release(), tmp_path, serving(routes))
    assert path.read_bytes() == EXE


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        ({**ROUTES, "MourseDecoder.exe": EXE + b"x"}, "passt nicht"),
        ({**ROUTES, "MourseDecoder.exe.sha256": b"keine summe"}, "ungültig"),
        ({**ROUTES, "MourseDecoder.exe.sha256": b""}, "ungültig"),
        ({k: v for k, v in ROUTES.items() if k != "MourseDecoder.exe"}, "No connection to GitHub"),
        ({"MourseDecoder.exe": EXE}, "No connection to GitHub"),
    ],
)
def test_bad_download_leaves_nothing_behind(tmp_path, routes, message):
    with pytest.raises(UpdateError, match=message):
        updater.download(release(), tmp_path, serving(routes))
    assert list(tmp_path.iterdir()) == []


def test_release_without_checksum_is_refused(tmp_path):
    with pytest.raises(UpdateError, match="no checksum"):
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
    foreign = Release(
        "v9.9.9", "", "https://evil.example/MourseDecoder.exe", GH + "x.sha256", "https://github.com/x", GH + "x.sig"
    )
    with pytest.raises(UpdateError, match="nicht von GitHub"):
        updater.download(foreign, tmp_path, serving({"x.sha256": SHA.encode(), "x.sig": SIG}))


def test_a_signature_from_another_host_is_refused(tmp_path):
    foreign = Release("v9.9.9", "", GH + "MourseDecoder.exe", GH + "x.sha256", "https://github.com/x", "https://evil.example/x.sig")
    with pytest.raises(UpdateError, match="nicht von GitHub"):
        updater.download(foreign, tmp_path, serving({"x.sha256": SHA.encode(), "x.sig": SIG}))
    assert list(tmp_path.iterdir()) == []


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
    with pytest.raises(UpdateError, match="größer als erwartet"):
        updater.download(release(), tmp_path, serving(ROUTES))
    assert list(tmp_path.iterdir()) == []


def test_a_download_that_is_no_windows_program_is_refused(tmp_path):
    text = b"#!/bin/sh\necho hallo\n"
    sha = hashlib.sha256(text).hexdigest()
    routes = {"MourseDecoder.exe": text, "MourseDecoder.exe.sha256": sha.encode(), "MourseDecoder.exe.sig": signature(sha=sha)}
    with pytest.raises(UpdateError, match="kein Windows-Programm"):
        updater.download(release(), tmp_path, serving(routes))
    assert list(tmp_path.iterdir()) == []


def test_plain_http_links_are_refused(tmp_path):
    insecure = Release(
        "v9.9.9", "", "http://example.org/MourseDecoder.exe", "http://example.org/x.sha256", "https://x", "http://example.org/x.sig"
    )
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
    assert str(Path("C:/it's/new.exe")).replace("'", "''") in script  # the path as the platform prints it
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
    updater.install(release(), serving(ROUTES), lambda *a, **kw: calls.append(a))
    script = decoded(calls[0][0])
    assert str(fake_exe) in script and "Start-Process" in script
    assert "mourse-update-" in script  # the verified download is what gets moved


@pytest.mark.skipif(sys.platform != "win32", reason="nur Windows kann sich selbst ersetzen")
def test_a_bad_checksum_never_starts_the_swap(tmp_path, monkeypatch):
    fake_exe = tmp_path / "MourseDecoder.exe"
    fake_exe.write_bytes(b"ALT")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    routes = {**ROUTES, "MourseDecoder.exe.sha256": b"0" * 64}
    with pytest.raises(UpdateError, match="passt nicht"):
        updater.install(release(), serving(routes), lambda *a, **kw: pytest.fail("darf nichts starten"))
    assert fake_exe.read_bytes() == b"ALT"


# --- update problems on a machine without git or an account ---


def failing(exc):
    def urlopen(request, timeout=None):
        raise exc

    return urlopen


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "x", {}, None)


class Broken(FakeResponse):
    def read(self, n=-1):
        raise http.client.IncompleteRead(b"x")


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (http_error(403), "limited the requests"),  # anonymous rate limit
        (http_error(429), "limited the requests"),
        (http_error(404), "error 404"),
        (urllib.error.URLError(ssl.SSLCertVerificationError("kaputt")), "secure connection"),
        (ssl.SSLError("handshake"), "secure connection"),
        (TimeoutError("timed out"), r"No connection to GitHub \(timed out"),
        (http.client.RemoteDisconnected("weg"), "No connection to GitHub"),
    ],
)
def test_network_problems_say_what_happened_and_offer_the_page(exc, message):
    with pytest.raises(UpdateError, match=message) as caught:
        updater.fetch_latest(failing(exc))
    assert caught.value.open_page


def test_an_answer_that_breaks_off_while_reading_is_reported():
    with pytest.raises(UpdateError, match="No connection") as caught:
        updater.fetch_latest(lambda request, timeout=None: Broken(b""))
    assert caught.value.open_page


def test_a_download_that_breaks_off_is_deleted_and_offers_the_page(tmp_path):
    def urlopen(request, timeout=None):
        name = request.full_url.rsplit("/", 1)[-1]
        return Broken(b"") if name == "MourseDecoder.exe" else FakeResponse(ROUTES[name])

    with pytest.raises(UpdateError, match="Download fehlgeschlagen") as caught:
        updater.download(release(), tmp_path, urlopen)
    assert caught.value.open_page and list(tmp_path.iterdir()) == []


def test_missing_checksum_or_exe_refuses_and_does_not_offer_a_download_by_hand(tmp_path):
    # whoever can edit a release can delete the checksum or the exe asset; a manual download would then be unchecked
    with pytest.raises(UpdateError, match="no checksum") as caught:
        updater.download(release(sha_url=None), tmp_path, serving({"MourseDecoder.exe": EXE}))
    assert not caught.value.open_page and list(tmp_path.iterdir()) == []
    assert "Do not download this release by hand" in str(caught.value)
    with pytest.raises(UpdateError, match="contains no MourseDecoder.exe") as caught:
        updater.fetch_latest(serving({"latest": json.dumps(release_json(assets=["x.zip"])).encode()}))
    assert not caught.value.open_page


def test_a_wrong_checksum_does_not_send_the_user_to_download_by_hand(tmp_path):
    with pytest.raises(UpdateError, match="passt nicht") as caught:
        updater.download(release(), tmp_path, serving({**ROUTES, "MourseDecoder.exe": EXE + b"x"}))
    assert not caught.value.open_page


def test_can_write_tells_a_writable_folder_from_a_missing_one(tmp_path):
    assert updater._can_write(tmp_path) and list(tmp_path.iterdir()) == []
    assert not updater._can_write(tmp_path / "gibt-es-nicht")


def frozen_at(monkeypatch, exe):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(exe))


def test_install_stops_before_downloading_when_the_exe_folder_is_not_writable(tmp_path, monkeypatch):
    frozen_at(monkeypatch, tmp_path / "MourseDecoder.exe")
    monkeypatch.setattr(updater, "_can_write", lambda folder: False)
    with pytest.raises(UpdateError, match="No permission to write") as caught:
        updater.install(release(), failing(AssertionError("darf nicht laden")), lambda *a, **kw: pytest.fail("start"))
    assert caught.value.open_page and str(tmp_path) in str(caught.value)


def test_powershell_that_cannot_start_is_reported_and_the_download_removed(tmp_path, monkeypatch):
    frozen_at(monkeypatch, tmp_path / "MourseDecoder.exe")
    made = []
    real_mkdtemp = updater.tempfile.mkdtemp
    monkeypatch.setattr(updater.tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])

    def blocked(*a, **kw):
        raise PermissionError("durch Richtlinie gesperrt")

    with pytest.raises(UpdateError, match="PowerShell could not be started") as caught:
        updater.install(release(), serving(ROUTES), blocked)
    assert caught.value.open_page and made and not Path(made[0]).exists()


def test_powershell_is_started_by_full_path_when_it_exists(tmp_path, monkeypatch):
    shell = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b"")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    calls = []
    updater.run_after_exit("exit 0", lambda *a, **kw: calls.append(a))
    assert calls[0][0][0] == str(shell)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "nichts"))  # no such file: fall back to the PATH lookup
    updater.run_after_exit("exit 0", lambda *a, **kw: calls.append(a))
    assert calls[1][0][0] == "powershell.exe"


# --- uninstall with the question about the own files ---


def test_uninstall_without_data_returns_no_folders_but_still_checks_the_name(tmp_path):
    assert updater.uninstall_targets(tmp_path / "MourseDecoder", delete_data=False) == []
    with pytest.raises(UpdateError, match="gehört nicht zum Programm"):
        updater.uninstall_targets(tmp_path / "Documents", delete_data=False)
    with pytest.raises(UpdateError):
        updater.uninstall(tmp_path / "Documents", lambda *a, **kw: pytest.fail("start"), delete_data=False)


def test_uninstall_script_without_folders_deletes_only_the_exe():
    script = updater.uninstall_script(Path("C:/App/MourseDecoder.exe"), [])
    assert "Remove-Item -LiteralPath $exe" in script and "-Recurse" not in script


def test_uninstall_script_with_folders_deletes_them_too():
    script = updater.uninstall_script(Path("C:/App/MourseDecoder.exe"), [Path("C:/Users/x/AppData/Roaming/MourseDecoder")])
    assert "-Recurse" in script and "MourseDecoder'" in script


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_uninstall_script_keeps_the_data_when_given_no_folders(tmp_path):
    exe, config = tmp_path / "MourseDecoder.exe", tmp_path / "MourseDecoder"
    exe.write_bytes(b"x")
    config.mkdir()
    (config / "settings.json").write_text("{}")
    assert powershell(updater.uninstall_script(exe, [], delay_s=0)) == 0
    assert not exe.exists() and (config / "settings.json").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="nur Windows nutzt das Skript")
def test_uninstall_from_the_exe_can_keep_the_data(tmp_path, monkeypatch):
    frozen_at(monkeypatch, tmp_path / "MourseDecoder.exe")
    calls = []
    updater.uninstall(tmp_path / "MourseDecoder", lambda *a, **kw: calls.append(a), delete_data=False)
    script = decoded(calls[0][0])
    assert "$exe" in script and "-Recurse" not in script and "PolyglotMorse" not in script


@pytest.mark.skipif(sys.platform != "win32", reason="nur Windows nutzt das Skript")
def test_uninstall_from_the_exe_with_data_removes_both_folders(tmp_path, monkeypatch):
    frozen_at(monkeypatch, tmp_path / "MourseDecoder.exe")
    calls = []
    updater.uninstall(tmp_path / "MourseDecoder", lambda *a, **kw: calls.append(a))
    script = decoded(calls[0][0])
    assert "$exe" in script and "PolyglotMorse'" in script and script.count("-Recurse") == 2


@pytest.mark.skipif(sys.platform != "win32", reason="nur Windows nutzt das Skript")
def test_uninstall_from_a_checkout_without_data_starts_nothing(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    updater.uninstall(tmp_path / "MourseDecoder", lambda *a, **kw: pytest.fail("nichts zu tun"), delete_data=False)


def test_uninstall_refuses_when_the_exe_cannot_be_deleted(tmp_path, monkeypatch):
    frozen_at(monkeypatch, tmp_path / "MourseDecoder.exe")
    monkeypatch.setattr(updater, "_can_write", lambda folder: False)
    with pytest.raises(UpdateError, match="No permission to write"):
        updater.uninstall(tmp_path / "MourseDecoder", lambda *a, **kw: pytest.fail("start"))


@pytest.mark.skipif(sys.platform == "win32", reason="Nicht-Windows löscht direkt")
def test_uninstall_elsewhere_keeps_the_data_on_request(tmp_path):
    config = tmp_path / "mourse-decoder"
    config.mkdir()
    updater.uninstall(config, delete_data=False)
    assert config.exists()


# --- signed updates ---

OTHER_SEED = signing.generate_seed()


class Recorder:
    """urlopen stand-in that also remembers which files were asked for."""

    def __init__(self, routes):
        self.asked = []
        self._serving = serving(routes)

    def __call__(self, request, timeout=None):
        self.asked.append(request.full_url.rsplit("/", 1)[-1])
        return self._serving(request, timeout)


def test_a_signed_release_installs_and_the_exe_is_kept(tmp_path):
    path = updater.download(release(), tmp_path, serving(ROUTES))
    assert path == tmp_path / "MourseDecoder.exe" and path.read_bytes() == EXE


@pytest.mark.parametrize(
    "sig",
    [
        signature(seed=OTHER_SEED),
        signature(tag="v9.9.8"),
        signature(name="Other.exe"),
        signature(sha="0" * 64),
        b"not base64 at all!",
        b"",
        base64.b64encode(b"x" * 63),
        SIG[:-5],
    ],
    ids=["other key", "other tag", "other file name", "other hash", "no base64", "empty", "63 bytes", "cut off"],
)
def test_a_wrong_signature_deletes_the_download(tmp_path, sig):
    ask = Recorder({**ROUTES, "MourseDecoder.exe.sig": sig})
    with pytest.raises(UpdateError, match="signature of the downloaded update does not match") as caught:
        updater.download(release(), tmp_path, ask)
    assert "MourseDecoder.exe" in ask.asked  # it was loaded and is gone again
    assert not caught.value.open_page and list(tmp_path.iterdir()) == []


def test_a_hijacked_release_with_a_matching_checksum_but_the_old_signature_is_refused(tmp_path):
    evil = b"MZ" + b"evil program"
    routes = {
        "MourseDecoder.exe": evil,
        "MourseDecoder.exe.sha256": hashlib.sha256(evil).hexdigest().encode(),  # an attacker publishes a fitting checksum
        "MourseDecoder.exe.sig": SIG,
    }
    with pytest.raises(UpdateError, match="does not match"):
        updater.download(release(), tmp_path, serving(routes))
    assert list(tmp_path.iterdir()) == []


def test_a_release_without_a_signature_is_refused_before_the_exe_is_loaded(tmp_path):
    ask = Recorder(ROUTES)
    with pytest.raises(UpdateError, match="has no signature") as caught:
        updater.download(release(sig_url=None), tmp_path, ask)
    # no download page: an attacker who deleted the .sig and swapped the exe must not get users to run it by hand
    assert not caught.value.open_page and ask.asked == [] and list(tmp_path.iterdir()) == []
    assert "Do not download this release by hand" in str(caught.value)


def test_a_signature_file_that_cannot_be_loaded_stops_the_update(tmp_path):
    ask = Recorder({k: v for k, v in ROUTES.items() if not k.endswith(".sig")})
    with pytest.raises(UpdateError, match="No connection to GitHub") as caught:
        updater.download(release(), tmp_path, ask)
    assert caught.value.open_page and "MourseDecoder.exe" not in ask.asked and list(tmp_path.iterdir()) == []


def test_a_signature_that_breaks_off_while_reading_is_reported(tmp_path):
    def urlopen(request, timeout=None):
        name = request.full_url.rsplit("/", 1)[-1]
        return Broken(b"") if name.endswith(".sig") else FakeResponse(ROUTES[name])

    with pytest.raises(UpdateError, match="No connection to GitHub") as caught:
        updater.download(release(), tmp_path, urlopen)
    assert caught.value.open_page and list(tmp_path.iterdir()) == []


def test_without_a_pinned_key_nothing_is_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", ())
    ask = Recorder(ROUTES)
    with pytest.raises(UpdateError, match="contains no key") as caught:
        updater.download(release(), tmp_path, ask)
    # kept: a build without a key is no release attack and has no other way to get an update
    assert caught.value.open_page and ask.asked == [] and list(tmp_path.iterdir()) == []


def test_every_pinned_key_counts_so_a_key_can_be_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (signing.public_key(OTHER_SEED), KEY))
    assert updater.download(release(), tmp_path, serving(ROUTES)).read_bytes() == EXE
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (signing.public_key(OTHER_SEED),))
    with pytest.raises(UpdateError, match="does not match"):
        updater.download(release(), tmp_path, serving(ROUTES))


def test_the_signature_is_read_with_a_small_cap(tmp_path):
    reads = []

    class Capped(FakeResponse):
        def read(self, n=-1):
            reads.append(n)
            return super().read(n)

    def urlopen(request, timeout=None):
        name = request.full_url.rsplit("/", 1)[-1]
        return (Capped if name.endswith(".sig") else FakeResponse)(ROUTES[name])

    updater.download(release(), tmp_path, urlopen)
    assert reads == [updater.MAX_SIG_BYTES + 1] and updater.MAX_SIG_BYTES <= 1024  # one more byte shows a too long file


def test_a_wrong_signature_never_starts_the_swap_and_removes_the_download_folder(tmp_path, monkeypatch):
    frozen_at(monkeypatch, tmp_path / "MourseDecoder.exe")
    made = []
    real_mkdtemp = updater.tempfile.mkdtemp
    monkeypatch.setattr(updater.tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])
    routes = {**ROUTES, "MourseDecoder.exe.sig": signature(seed=OTHER_SEED)}
    with pytest.raises(UpdateError, match="does not match"):
        updater.install(release(), serving(routes), lambda *a, **kw: pytest.fail("darf nichts starten"))
    assert made and not Path(made[0]).exists()


# --- no downgrade, even with a valid signature ---


@pytest.mark.parametrize("tag", [f"v{__version__}", "v.0.0.1", "v0.1.8", "kein-tag"], ids=["same", "very old", "older", "no version"])
def test_an_older_or_equal_release_is_never_installed_even_when_correctly_signed(tmp_path, monkeypatch, tag):
    old = Release(tag, "", GH + "MourseDecoder.exe", GH + "MourseDecoder.exe.sha256", "https://github.com/x", GH + "MourseDecoder.exe.sig")
    ask = Recorder({**ROUTES, "MourseDecoder.exe.sig": signature(tag=tag)})  # the pinned key did sign this one
    with pytest.raises(UpdateError, match="not newer") as caught:
        updater.download(old, tmp_path, ask)
    assert tag in str(caught.value) and not caught.value.open_page
    frozen_at(monkeypatch, tmp_path / "MourseDecoder.exe")
    with pytest.raises(UpdateError, match="not newer"):
        updater.install(old, ask, lambda *a, **kw: pytest.fail("darf nichts starten"))
    assert ask.asked == [] and list(tmp_path.iterdir()) == []  # nothing was even loaded


# --- small files that are too long ---


@pytest.mark.parametrize("name", ["MourseDecoder.exe.sig", "MourseDecoder.exe.sha256"])
def test_a_small_file_that_is_too_long_is_an_error_and_not_cut_off(tmp_path, name):
    padded = ROUTES[name] + b" " * 2000  # a valid first part: cutting it at the limit would let it pass
    ask = Recorder({**ROUTES, name: padded})
    with pytest.raises(UpdateError, match="much larger than expected") as caught:
        updater.download(release(), tmp_path, ask)
    assert name in str(caught.value) and "MourseDecoder.exe" not in ask.asked and list(tmp_path.iterdir()) == []


def test_a_small_file_of_exactly_the_limit_is_still_read(tmp_path):
    padded = {name: ROUTES[name].ljust(limit) for name, limit in (("MourseDecoder.exe.sig", updater.MAX_SIG_BYTES), ("MourseDecoder.exe.sha256", updater.MAX_SHA_BYTES))}
    assert updater.download(release(), tmp_path, serving({**ROUTES, **padded})).read_bytes() == EXE


# --- the file is checked again by the script right before the swap ---

DIGEST = "ab" * 32


def test_update_script_hashes_the_file_before_it_touches_the_program():
    script = updater.update_script(Path("C:/T/mourse-update-x/n.exe"), Path("C:/A/a.exe"), sha256=DIGEST.upper())
    assert f"(Get-FileHash -LiteralPath $new -Algorithm SHA256).Hash -ne '{DIGEST}'" in script  # lower-cased literal
    loop = script.split("for ($i")[1]
    first, second, third = (m.start() for m in re.finditer(r"Get-FileHash", loop.split("if ($bad)")[0]))
    rename, move = loop.index("File]::Move($target, $old)"), loop.index("File]::Move($new, $target)")
    assert first < rename < second < move < third  # before the rename, before the move, on the moved-in file
    assert "-LiteralPath $target -Algorithm" in loop[third : third + 60]
    assert "-and -not $bad" in script.split("for ($i")[1].split("\n")[0]  # a mismatch ends the retry loop


def test_update_script_never_deletes_the_program_before_the_new_one_is_in_place():
    script = updater.update_script(Path("C:/T/n.exe"), Path("C:/A/a.exe"), sha256=DIGEST)
    assert f"$old = '{Path('C:/A/a.exe')}.old'" in script  # the path as the platform prints it
    deletes = [m.start() for m in re.finditer(r"File\]::Delete\(\$target\)", script)]
    placed = script.index("$placed = $true; [System.IO.File]::Move($new, $target)")
    assert deletes and all(d > placed for d in deletes)  # only the undo of a placed file deletes the target
    assert script.index("File]::Delete($old)") < script.index("File]::Move($target, $old)")  # clear an old leftover first
    assert script.index("File]::Move($old, $target)") > placed  # the undo puts the backup back


def test_update_script_leaves_the_program_alone_when_the_hash_differs():
    script = updater.update_script(Path("C:/T/mourse-update-x/n.exe"), Path("C:/A/a.exe"), status=Path("C:/C/r.txt"), sha256=DIGEST)
    mismatch = script.split("if ($bad) {")[1].split("exit 3")[0]
    assert "Report 'verify'" in mismatch and "Remove-Item -LiteralPath" in mismatch  # the changed file is deleted
    assert "Start-Process -FilePath $target" in mismatch  # the old program runs again
    assert "File]::Delete" not in mismatch and "File]::Move" not in mismatch
    assert "Get-FileHash" not in updater.update_script(Path("C:/T/n.exe"), Path("C:/A/a.exe"))  # no digest, no check


@pytest.mark.parametrize(
    "bad",
    ["", "ab" * 31, "ab" * 33, "zz" * 32, DIGEST + "'; calc; '", "ab" * 31 + "a'", " " + DIGEST, DIGEST + "\n", "\u2019" * 64],
)
def test_a_digest_that_is_not_64_hex_digits_never_reaches_the_script(bad):
    with pytest.raises(ValueError):
        updater.update_script(Path("C:/T/n.exe"), Path("C:/A/a.exe"), sha256=bad)


def test_install_hands_the_checked_hash_to_the_script(tmp_path, monkeypatch):
    frozen_at(monkeypatch, tmp_path / "MourseDecoder.exe")
    made = []
    real_mkdtemp = updater.tempfile.mkdtemp
    monkeypatch.setattr(updater.tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])
    scripts = []
    monkeypatch.setattr(updater, "run_after_exit", lambda script, popen=None: scripts.append(script))
    try:
        updater.install(release(), serving(ROUTES))
    finally:
        shutil.rmtree(made[0], ignore_errors=True)  # the real script would remove it
    assert f"-ne '{SHA}'" in scripts[0]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_update_script_with_the_right_hash_replaces_the_exe(tmp_path):
    new, target = tmp_path / "neu.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    digest = hashlib.sha256(b"NEU").hexdigest()
    assert powershell(updater.update_script(new, target, start=False, delay_s=0, sha256=digest)) == 0
    assert target.read_bytes() == b"NEU" and not new.exists()
    assert not backup_of(target).exists()  # the old exe is gone only because the new one is in place


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_update_script_does_not_install_a_file_that_changed_after_the_download_check(tmp_path):
    folder = tmp_path / (updater.TEMP_PREFIX + "tamper")
    folder.mkdir()
    new, target = folder / "MourseDecoder.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    digest = hashlib.sha256(b"NEU").hexdigest()
    new.write_bytes(b"EVIL")  # swapped in the temp folder after download() had verified it
    target.write_bytes(b"ALT")
    status = tmp_path / "cfg" / updater.RESULT_FILE
    assert powershell(updater.update_script(new, target, start=False, delay_s=0, status=status, sha256=digest)) == 3
    assert target.read_bytes() == b"ALT"  # the program is still there
    assert not folder.exists() and status.read_bytes() == b"verify"
    assert not backup_of(target).exists()


# --- the swap never leaves the user without a working exe ---


def backup_of(exe: Path) -> Path:
    return exe.with_name(exe.name + updater.BACKUP_SUFFIX)


def start_powershell(script: str) -> subprocess.Popen:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])


def wait_for(path: Path, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.001)
    return False


def overwrite(path: Path, data: bytes) -> None:
    """Change a file the script may be reading right now (a read in progress denies the write for a moment)."""
    end = time.monotonic() + 10
    while True:
        try:
            with path.open("r+b") as handle:
                handle.write(data)
            return
        except PermissionError:
            if time.monotonic() > end:
                raise
            time.sleep(0.005)


@contextlib.contextmanager
def held_like_a_running_exe(path: Path):
    """Others may read, rename and delete the file but not open it for writing, as with a running exe."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID] + [wintypes.DWORD] * 2 + [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0x1 | 0x4, None, 3, 0x80, None)  # read; share read+delete; existing
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


win = pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")


@win
def test_a_swap_that_cannot_finish_gives_the_old_exe_back_byte_for_byte(tmp_path):
    new, target = tmp_path / "neu.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT\x00\xff")
    status = tmp_path / "cfg" / updater.RESULT_FILE
    script = updater.update_script(new, target, start=False, delay_s=0, tries=3, status=status, sha256=hashlib.sha256(b"NEU").hexdigest())
    with new.open("rb"):  # readable and hashable, but it cannot be moved: the hash checks pass, the Move fails
        assert powershell(script) == 1
    assert target.read_bytes() == b"ALT\x00\xff" and not backup_of(target).exists()
    assert new.read_bytes() == b"NEU" and status.read_bytes() == b"busy"


@win
def test_a_file_changed_between_the_rename_and_the_move_restores_the_old_exe(tmp_path):
    new, target = tmp_path / "neu.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT\x00\xff")
    status = tmp_path / "cfg" / updater.RESULT_FILE
    runner = start_powershell(
        updater.update_script(new, target, start=False, delay_s=0, status=status, sha256=hashlib.sha256(b"NEU").hexdigest())
    )
    try:
        with new.open("rb"):  # every Move fails, so the script keeps renaming and restoring
            assert wait_for(backup_of(target))  # the old exe has just been renamed: the new file is still unmoved
            overwrite(new, b"EVIL")
            assert runner.wait(timeout=60) == 3
    finally:
        if runner.poll() is None:
            runner.kill()
    assert target.read_bytes() == b"ALT\x00\xff" and not backup_of(target).exists()
    assert new.read_bytes() == b"EVIL" and status.read_bytes() == b"verify"


@win
def test_a_lock_that_goes_away_during_the_swap_lets_the_update_finish(tmp_path):
    new, target = tmp_path / "neu.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    runner = start_powershell(updater.update_script(new, target, start=False, delay_s=0, sha256=hashlib.sha256(b"NEU").hexdigest()))
    try:
        with new.open("rb"):
            assert wait_for(backup_of(target))  # a round has started its swap and will fail on the Move
        assert runner.wait(timeout=60) == 0
    finally:
        if runner.poll() is None:
            runner.kill()
    assert target.read_bytes() == b"NEU" and not backup_of(target).exists() and not new.exists()


@win
def test_a_running_exe_that_could_be_renamed_is_still_waited_for(tmp_path):
    new, target = tmp_path / "neu.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    runner = start_powershell(updater.update_script(new, target, start=False, delay_s=0))
    try:
        with held_like_a_running_exe(target):
            with pytest.raises(subprocess.TimeoutExpired):
                runner.wait(timeout=3)
            assert target.read_bytes() == b"ALT" and not backup_of(target).exists()  # not renamed while it runs
        assert runner.wait(timeout=60) == 0
    finally:
        if runner.poll() is None:
            runner.kill()
    assert target.read_bytes() == b"NEU" and not backup_of(target).exists()


@win
def test_update_installs_when_there_is_no_exe_yet_and_clears_an_old_leftover(tmp_path):
    new, target = tmp_path / "neu.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    backup_of(target).write_bytes(b"LEFTOVER")  # no exe: nothing is renamed, the leftover goes after the success
    assert powershell(updater.update_script(new, target, start=False, delay_s=0)) == 0
    assert target.read_bytes() == b"NEU" and not backup_of(target).exists()
    new.write_bytes(b"NEU2")
    backup_of(target).write_bytes(b"LEFTOVER")  # an exe and a leftover of an earlier update
    assert powershell(updater.update_script(new, target, start=False, delay_s=0)) == 0
    assert target.read_bytes() == b"NEU2" and not backup_of(target).exists()


@win
def test_uninstall_also_deletes_a_leftover_backup_of_the_exe(tmp_path):
    exe = tmp_path / "MourseDecoder.exe"
    exe.write_bytes(b"x")
    backup_of(exe).write_bytes(b"y")
    keep = tmp_path / "MourseDecoder.exe.txt"
    keep.write_bytes(b"z")
    assert powershell(updater.uninstall_script(exe, [], delay_s=0)) == 0
    assert not exe.exists() and not backup_of(exe).exists() and keep.exists()
    exe.write_bytes(b"x")
    backup_of(exe).write_bytes(b"y")
    with backup_of(exe).open("rb"):  # what stays behind is reported
        assert powershell(updater.uninstall_script(exe, [], delay_s=0)) == 1


# --- a swap that cannot be undone never starts the file that is in place ---


def test_update_script_stops_without_starting_anything_when_the_undo_failed():
    script = updater.update_script(Path("C:/T/n.exe"), Path("C:/A/a.exe"), status=Path("C:/C/r.txt"), sha256=DIGEST)
    marker = "if (-not $done -and ($placed -or $renamed)) {"
    block = script.split(marker)[1].split("}")[0]
    assert "Report 'rollback'" in block and "exit 4" in block and "Start-Process" not in block
    assert script.index(marker) < script.index("if ($bad) {") < script.index("\nif (-not $done) {\n")  # before both restarts
    assert marker in updater.update_script(Path("C:/T/n.exe"), Path("C:/A/a.exe"))  # also without a hash check
    # a round whose undo failed is not repeated: the next one would delete the backup of the old exe
    assert "-and -not ($placed -or $renamed); $i++)" in script.split("for ($i")[1].split("\n")[0]


def marker_app(marker: Path, word: str) -> bytes:
    """A harmless stand-in for the exe: a batch file that writes ``word`` into ``marker`` when it is started."""
    return f'@echo {word}>"{marker}"\r\n'.encode()


def paused_after_the_move(script: str) -> str:
    """The same script with a pause after the move and 3 undo tries instead of 40, so a test can change the
    placed file before it is hashed again and does not wait 20 s for the undo."""
    move = "$placed = $true; [System.IO.File]::Move($new, $target)"
    assert move in script and "$r -lt 40" in script
    return script.replace(move, move + "; Start-Sleep -Seconds 2").replace("$r -lt 40", "$r -lt 3")


def file_has(path: Path, data: bytes) -> bool:
    try:
        return path.read_bytes() == data
    except OSError:
        return False


def wait_until(condition, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False


@win
@pytest.mark.parametrize("undo_blocked", [False, True], ids=["undo works", "undo cannot delete the placed file"])
def test_a_file_that_changes_after_the_move_is_never_started_when_it_cannot_be_removed(tmp_path, undo_blocked):
    marker, status = tmp_path / "started.txt", tmp_path / "cfg" / updater.RESULT_FILE
    old, good, evil = (marker_app(marker, word) for word in ("old", "new", "bad"))  # same length, different hash
    new, target = tmp_path / "new.cmd", tmp_path / "app.cmd"
    new.write_bytes(good)
    target.write_bytes(old)
    script = updater.update_script(new, target, delay_s=0, status=status, sha256=hashlib.sha256(good).hexdigest())
    runner = start_powershell(paused_after_the_move(script))
    try:
        assert wait_until(lambda: file_has(target, good))  # moved in and verified once, now the script pauses
        overwrite(target, evil)  # changed after the pre-move check, before the hash of the placed file
        if undo_blocked:
            with target.open("rb"):  # no share-delete: the script cannot delete the placed file
                code = runner.wait(timeout=60)
        else:
            code = runner.wait(timeout=60)
    finally:
        if runner.poll() is None:
            runner.kill()
    if undo_blocked:
        assert code == 4 and status.read_bytes() == b"rollback"
        assert target.read_bytes() == evil and backup_of(target).read_bytes() == old  # the old exe is kept for recovery
        assert not wait_until(marker.exists, timeout=3)  # the file in place was not started
    else:
        assert code == 3 and status.read_bytes() == b"verify"
        assert target.read_bytes() == old and not backup_of(target).exists()
        assert wait_until(lambda: file_has(marker, b"old\r\n"))  # the restored old program was started, not the evil one
