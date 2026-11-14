"""Review round D: PowerShell quoting, failed updates, literal paths, junctions, redirects."""

from __future__ import annotations

import base64
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from mourse_decoder import updater
from mourse_decoder.i18n import set_language
from mourse_decoder.updater import UpdateError

win = pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language("en")


def powershell(script: str) -> int:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], timeout=60).returncode


def parse_errors(script_file: Path) -> tuple[int, int]:
    """(syntax errors, command calls other than the known ones) by PowerShell's own parser; nothing is executed."""
    code = (
        "$e=$null;$t=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script_file}',[ref]$t,[ref]$e);"
        "$names=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.CommandAst]},$true)|"
        "ForEach-Object{$_.GetCommandName()};"
        "Write-Output $e.Count;Write-Output ($names -join ',')"
    )
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code], capture_output=True, text=True, timeout=60
    ).stdout.split()
    errors = int(out[0])
    names = set(out[1].split(",")) if len(out) > 1 else set()
    known = {"Start-Sleep", "Remove-Item", "Start-Process", "New-Item", "Split-Path", "New-Object", "Get-Item", "Where-Object", "Test-Path", "Out-Null", "Report", "Get-FileHash"}
    return errors, len(names - known)


# --- 1: typographic quotes ---

TRICKY = ["D\u2019Arcy", "x\u2019;calc;\u2019", "a\u2018b", "a\u201ab", "a\u201bb", "it's", "plain"]


@pytest.mark.parametrize("name", TRICKY)
def test_quote_doubles_every_powershell_quote(name):
    quoted = updater._quote(name)
    inner = quoted[1:-1]
    for ch in updater._PS_QUOTES:
        assert inner.count(ch) == 2 * name.count(ch)


@win
@pytest.mark.parametrize("name", TRICKY[:-1])
def test_scripts_with_typographic_quotes_parse_cleanly_and_call_nothing_extra(tmp_path, name):
    new = tmp_path / f"mourse-update-{name}" / "new.exe"
    target = tmp_path / name / "MourseDecoder.exe"
    scripts = {
        "update": updater.update_script(new, target, status=tmp_path / name / "r.txt"),
        "update with hash check": updater.update_script(new, target, status=tmp_path / name / "r.txt", sha256="ab" * 32),
        "uninstall": updater.uninstall_script(target, [tmp_path / name / "MourseDecoder"], status=tmp_path / name / "r.txt"),
        "msi update": updater.msi_update_script(
            new.with_suffix(".msi"), target, tmp_path / name / "r.txt", "ab" * 32, tmp_path / name / "msi-update.log"
        ),
        "msi uninstall": updater.msi_uninstall_script(
            target, "{6F9619FF-8B86-D011-B42D-00C04FC964FF}", [tmp_path / name / "MourseDecoder"], status=tmp_path / name / "r.txt"
        ),
    }
    for kind, script in scripts.items():
        file = tmp_path / f"{kind}.ps1"
        file.write_text(script, encoding="utf-8-sig")
        errors, extra = parse_errors(file)
        assert errors == 0, (kind, name)
        assert extra == 0, (kind, name)


@win
def test_update_works_in_a_folder_with_typographic_quotes(tmp_path):
    folder = tmp_path / "x\u2019;calc;\u2019"
    folder.mkdir()
    new, target = folder / "new.exe", folder / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    assert powershell(updater.update_script(new, target, start=False, delay_s=0)) == 0
    assert target.read_bytes() == b"NEU"


# --- 2: failed update ---


def test_failed_update_script_restarts_cleans_up_and_reports():
    script = updater.update_script(
        Path("C:/Temp/mourse-update-ab/MourseDecoder.exe"), Path("C:/App/MourseDecoder.exe"), status=Path("C:/Cfg/update-result.txt")
    )
    failure = script.split("\nif (-not $done) {\n")[1].split("exit 1")[0]  # the block after the loop
    assert "Report 'busy'" in failure
    assert "Start-Process -FilePath $target" in failure
    assert f"Remove-Item -LiteralPath '{Path('C:/Temp/mourse-update-ab')}'" in failure  # as the platform prints it


def test_failed_update_without_start_does_not_start_anything():
    script = updater.update_script(Path("C:/T/n.exe"), Path("C:/A/a.exe"), start=False)
    assert "Start-Process" not in script


@win
def test_failed_update_writes_the_status_file_and_removes_the_temp_folder(tmp_path):
    download = tmp_path / "mourse-update-xyz"
    download.mkdir()
    new, target = download / "MourseDecoder.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    status = tmp_path / "cfg" / updater.RESULT_FILE
    with target.open("rb"):
        code = powershell(updater.update_script(new, target, start=False, delay_s=0, tries=2, status=status))
    assert code == 1
    assert status.read_bytes() == b"busy"  # no BOM, one code
    assert not download.exists()
    assert target.read_bytes() == b"ALT"


@win
def test_successful_update_removes_an_old_status_file(tmp_path):
    new, target = tmp_path / "n.exe", tmp_path / "MourseDecoder.exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    status = tmp_path / updater.RESULT_FILE
    status.write_text("busy")
    assert powershell(updater.update_script(new, target, start=False, delay_s=0, status=status)) == 0
    assert not status.exists()


def test_read_update_result_returns_text_and_clears_the_file(tmp_path):
    (tmp_path / updater.RESULT_FILE).write_text("busy", encoding="utf-8")
    text = updater.read_update_result(tmp_path)
    assert text == updater.tr("update.result.busy") and "stayed in use" in text
    assert not (tmp_path / updater.RESULT_FILE).exists()
    assert updater.read_update_result(tmp_path) is None


def test_read_update_result_knows_every_code_and_survives_garbage(tmp_path):
    for code in ("start", "uninstall", "reparse", "verify", "rollback", "msi"):
        (tmp_path / updater.RESULT_FILE).write_text(code + "\n", encoding="utf-8-sig")
        assert updater.read_update_result(tmp_path) == updater.tr(f"update.result.{code}")
    (tmp_path / updater.RESULT_FILE).write_bytes(b"\xff\xfe{x}")
    assert updater.read_update_result(tmp_path) in (updater.tr("update.result.unknown"), None)
    (tmp_path / updater.RESULT_FILE).write_text("zzz")
    assert updater.read_update_result(tmp_path) == updater.tr("update.result.unknown")


def test_install_passes_the_status_file_to_the_script(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "MourseDecoder.exe"))
    monkeypatch.setattr(updater, "result_path", lambda config_dir=None: tmp_path / "cfg" / updater.RESULT_FILE)
    monkeypatch.setattr(updater, "_download", lambda rel, folder, urlopen=None: (folder / "MourseDecoder.exe", "ab" * 32))
    scripts = []
    monkeypatch.setattr(updater, "run_after_exit", lambda script, popen=None: scripts.append(script))
    updater.install(updater.Release("v9", "", "https://github.com/e", None, "https://github.com/p"))
    assert "update-result.txt" in scripts[0] and "-ne '" + "ab" * 32 + "'" in scripts[0]


# --- 3: [ ] in paths ---


@win
def test_update_replaces_an_exe_in_a_folder_with_brackets(tmp_path):
    folder = tmp_path / "App [1]"
    folder.mkdir()
    new, target = tmp_path / "new[2].exe", folder / "MourseDecoder[1].exe"
    new.write_bytes(b"NEU")
    target.write_bytes(b"ALT")
    assert powershell(updater.update_script(new, target, start=False, delay_s=0)) == 0
    assert target.read_bytes() == b"NEU" and not new.exists()
    assert [p.name for p in folder.iterdir()] == ["MourseDecoder[1].exe"]


# --- 4: junction as the data folder ---


@win
def test_uninstall_skips_a_junction_and_leaves_its_target_alone(tmp_path):
    real = tmp_path / "wichtig"
    real.mkdir()
    (real / "datei.txt").write_text("bleibt")
    appdata = tmp_path / "roaming"
    appdata.mkdir()
    link = appdata / "MourseDecoder"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("Junction nicht möglich")
    status = tmp_path / "cfg" / updater.RESULT_FILE
    assert powershell(updater.uninstall_script(None, [link], delay_s=0, status=status)) == 1
    assert (real / "datei.txt").read_text() == "bleibt"
    assert link.exists()
    assert status.read_bytes() == b"reparse"
    subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)


@win
def test_uninstall_still_deletes_a_normal_folder_and_reports_leftovers(tmp_path):
    config = tmp_path / "MourseDecoder"
    config.mkdir()
    (config / "a.txt").write_text("x")
    status = tmp_path / "st.txt"
    assert powershell(updater.uninstall_script(None, [config], delay_s=0, status=status)) == 0
    assert not config.exists() and not status.exists()
    exe = tmp_path / "MourseDecoder.exe"
    exe.write_bytes(b"x")
    with exe.open("rb"):
        assert powershell(updater.uninstall_script(exe, [], delay_s=0, tries=2, status=status)) == 1
    assert status.read_bytes() == b"uninstall"


# --- 5: redirects ---


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/a/b",
        "https://api.github.com/repos/x",
        "https://objects.githubusercontent.com/x",
        "https://release-assets.githubusercontent.com/x",
    ],
)
def test_allowed_hosts(url):
    assert updater._allowed_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a",
        "https://evil.example/github.com",
        "https://github.com.evil.example/x",
        "https://github.com@evil.example/x",
        "https://evilgithubusercontent.com/x",
        "ftp://github.com/x",
        "https://[bad/x",
        "",
    ],
)
def test_refused_hosts(url):
    assert not updater._allowed_url(url)


def redirect(newurl: str):
    handler = updater._redirect_handler()
    request = urllib.request.Request("https://github.com/x")
    return handler.redirect_request(request, None, 302, "Found", {}, newurl)


def test_redirect_within_github_is_followed():
    assert redirect("https://objects.githubusercontent.com/file").full_url == "https://objects.githubusercontent.com/file"


@pytest.mark.parametrize("newurl", ["http://github.com/x", "https://evil.example/x", "http://evil.example/x"])
def test_redirect_hop_to_http_or_foreign_host_is_refused(newurl):
    with pytest.raises(UpdateError, match="umgeleitet"):
        redirect(newurl)


def test_open_uses_the_checking_opener_by_default(monkeypatch):
    seen = []

    class Opener:
        def open(self, request, timeout=None):
            seen.append(request.full_url)
            raise OSError("stop")

    built = []
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: built.append(handlers) or Opener())
    with pytest.raises(UpdateError):
        updater._open("https://github.com/x", None)
    assert seen == ["https://github.com/x"]
    assert type(built[0][0]).__name__ == "SafeRedirect"
