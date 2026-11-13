"""Tests for packaging/sign_release.py (offline release signing)."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from mourse_decoder import signing

SCRIPT = Path(__file__).resolve().parents[2] / "packaging" / "sign_release.py"
_spec = importlib.util.spec_from_file_location("sign_release", SCRIPT)
sign_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sign_release)

TAG = "v.0.1.9"
EXE_BYTES = b"MZ fake exe for the signing tests" * 100
# the real PINNED_PUBLIC_KEYS holds the maintainer's key; tests that need a pinned key patch the tuple
SKIP_FLAGS = ("--allow-unpinned", "--no-git-check")


@pytest.fixture
def key_file(tmp_path, capsys):
    path = tmp_path / "update.key"
    assert sign_release.main(["keygen", "--out", str(path)]) == 0
    capsys.readouterr()
    return path


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "downloaded-build.exe"
    path.write_bytes(EXE_BYTES)
    return path


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A throw-away repository with the tag TAG on its first commit; sign_release looks for tags there."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        base = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
        subprocess.run([*base, *args], check=True, capture_output=True)

    git("init", "-q")
    git("commit", "--allow-empty", "-q", "-m", "one")
    git("tag", TAG)
    monkeypatch.setattr(sign_release, "ROOT", repo)
    return git


def seed_of(key_file):
    return bytes.fromhex(key_file.read_text(encoding="ascii").strip())


def sign_args(exe, key_file, tag=TAG, *extra):
    return ["sign", "--tag", tag, "--exe", str(exe), "--key", str(key_file), *extra]


def run_sign(exe, key_file, tag=TAG, *extra):
    """sign without the pinned-key and git checks (those have their own tests)."""
    return sign_release.main(sign_args(exe, key_file, tag, *SKIP_FLAGS, *extra))


def run_verify(exe, sig, tag=TAG, pubkey=None):
    return sign_release.main(
        ["verify", "--tag", tag, "--exe", str(exe), "--sig", str(sig), "--pubkey", pubkey.hex(), "--no-git-check"]
    )


def test_keygen_prints_only_the_public_key(tmp_path, capsys):
    path = tmp_path / "update.key"
    assert sign_release.main(["keygen", "--out", str(path)]) == 0
    captured = capsys.readouterr()
    seed_hex = path.read_text(encoding="ascii").strip()
    assert len(bytes.fromhex(seed_hex)) == 32
    assert captured.out.strip() == signing.public_key(bytes.fromhex(seed_hex)).hex()
    assert seed_hex not in captured.out + captured.err


def test_keygen_refuses_existing_file(tmp_path, capsys):
    path = tmp_path / "update.key"
    path.write_text("keep me", encoding="ascii")
    assert sign_release.main(["keygen", "--out", str(path)]) == 1
    assert path.read_text(encoding="ascii") == "keep me"
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("relative", ["new.key", "packaging/new.key", "packaging/../new.key"])
def test_keygen_refuses_path_inside_the_repository(relative, capsys):
    path = (sign_release.ROOT / relative).resolve()
    assert not path.exists()
    try:
        assert sign_release.main(["keygen", "--out", str(sign_release.ROOT / relative)]) == 1
        assert not path.exists()
    finally:
        # a regression of the guard must not leave key material in the repository
        path.unlink(missing_ok=True)
    captured = capsys.readouterr()
    assert "inside the repository" in captured.err
    assert captured.out == ""


def test_keygen_restricts_the_file_before_the_seed_is_written(tmp_path, monkeypatch):
    path = tmp_path / "update.key"
    seen = []
    monkeypatch.setattr(sign_release, "restrict_to_owner", lambda target: seen.append(target.read_text(encoding="ascii")))
    assert sign_release.main(["keygen", "--out", str(path)]) == 0
    assert seen == [""]
    assert len(bytes.fromhex(path.read_text(encoding="ascii").strip())) == 32


windows_only = pytest.mark.skipif(os.name != "nt", reason="icacls is a Windows tool")


ONE_ENTRY = "C:\\k\\update.key PC\\user:(F)\n\nSuccessfully processed 1 files; Failed processing 0 files\n"


def _fake_run(
    calls, whoami_output='"PC\\user","S-1-5-21-1-2-3-1001"\n', whoami_fails=False, fails=(), listing=ONE_ENTRY
):
    """Stands in for subprocess.run; `fails` names the icacls steps that exit non-zero: reset, grant or show."""

    def fake(cmd, **kwargs):
        calls.append(cmd)
        if Path(cmd[0]).name.lower() == "whoami.exe":
            if whoami_fails:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=whoami_output, stderr="")
        step = "reset" if "/reset" in cmd else "grant" if "/grant:r" in cmd else "show"
        if step in fails:
            raise subprocess.CalledProcessError(5, cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=listing if step == "show" else "", stderr="")

    return fake


@windows_only
def test_restrict_calls_icacls_and_whoami_by_full_path_and_grants_by_sid(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sign_release.subprocess, "run", _fake_run(calls))
    target = tmp_path / "update.key"
    sign_release.restrict_to_owner(target)
    whoami, reset, grant, show = calls
    assert Path(whoami[0]).is_absolute() and Path(whoami[0]).name.lower() == "whoami.exe"
    for icacls in (reset, grant, show):
        assert Path(icacls[0]).is_absolute() and Path(icacls[0]).name.lower() == "icacls.exe"
    assert reset[1:] == [str(target), "/reset"]  # its own call: in one call with the others it has no effect
    assert grant[1:] == [str(target), "/inheritance:r", "/grant:r", "*S-1-5-21-1-2-3-1001:F"]
    assert show[1:] == [str(target)]
    assert capsys.readouterr().err == ""


@windows_only
@pytest.mark.parametrize("whoami_output", ["", "garbage\n", '"PC\\user","not-a-sid"\n'])
def test_restrict_warns_on_an_unusable_whoami_answer(tmp_path, monkeypatch, capsys, whoami_output):
    monkeypatch.setattr(sign_release.subprocess, "run", _fake_run([], whoami_output))
    sign_release.restrict_to_owner(tmp_path / "update.key")
    assert "could not restrict access" in capsys.readouterr().err


@windows_only
def test_restrict_warns_when_whoami_fails_and_keygen_still_writes_the_key(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sign_release.subprocess, "run", _fake_run(calls, whoami_fails=True))
    path = tmp_path / "update.key"
    assert sign_release.main(["keygen", "--out", str(path)]) == 0
    assert "could not restrict access" in capsys.readouterr().err
    assert len(calls) == 1  # no SID, so no icacls call
    assert len(bytes.fromhex(path.read_text(encoding="ascii").strip())) == 32


@windows_only
def test_restrict_still_grants_and_warns_when_the_reset_fails(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sign_release.subprocess, "run", _fake_run(calls, fails=("reset",)))
    sign_release.restrict_to_owner(tmp_path / "update.key")
    assert "could not restrict access" in capsys.readouterr().err
    assert [c for c in calls if "/grant:r" in c]  # the grant is still tried


@windows_only
def test_restrict_warns_and_does_not_raise_when_the_grant_fails(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sign_release.subprocess, "run", _fake_run(calls, fails=("grant",)))
    sign_release.restrict_to_owner(tmp_path / "update.key")
    assert "could not restrict access" in capsys.readouterr().err
    assert len(calls) == 3  # whoami, reset, grant; the result is not checked after a failed grant


@windows_only
@pytest.mark.parametrize(
    "listing",
    [
        "C:\\k\\update.key NT AUTHORITY\\SYSTEM:(F)\n               PC\\user:(F)\n",
        "C:\\k\\update.key PC\\user:(I)(F)\n               BUILTIN\\Administrators:(F)\n               OWNER RIGHTS:(F)\n",
        "",
    ],
)
def test_restrict_warns_when_other_access_entries_remain(tmp_path, monkeypatch, capsys, listing):
    monkeypatch.setattr(sign_release.subprocess, "run", _fake_run([], listing=listing))
    sign_release.restrict_to_owner(tmp_path / "update.key")
    assert "access entries remain" in capsys.readouterr().err


@windows_only
def test_restrict_warns_when_the_result_cannot_be_read_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sign_release.subprocess, "run", _fake_run([], fails=("show",)))
    sign_release.restrict_to_owner(tmp_path / "update.key")
    assert "could not restrict access" in capsys.readouterr().err


def _icacls(*args):
    return subprocess.run([sign_release._system32("icacls.exe"), *map(str, args)], capture_output=True, text=True)


def _folder(tmp_path, inheritable):
    """A normal folder, or one like the runner's pytest tmp dir: no inheritable entries, so a new file keeps the
    explicit SYSTEM, Administrators and OWNER RIGHTS entries from its creator's default DACL."""
    folder = tmp_path / ("inheriting" if inheritable else "plain")
    folder.mkdir()
    if not inheritable:
        sid = sign_release._current_user_sid()
        _icacls(folder, "/inheritance:r", "/grant:r", f"*{sid}:F")  # no (OI)(CI): the entry is not inherited
    return folder


@windows_only
@pytest.mark.parametrize("inheritable", [True, False], ids=["folder-with-inheritable-entries", "folder-without"])
def test_restrict_leaves_exactly_one_access_entry(tmp_path, inheritable):
    path = _folder(tmp_path, inheritable) / "update.key"
    path.write_text("x", encoding="ascii")
    sign_release.restrict_to_owner(path)
    shown = _icacls(path).stdout
    assert shown.count(":(") == 1, shown
    assert "(F)" in shown


@windows_only
def test_restrict_reports_nothing_on_a_folder_without_inheritable_entries(tmp_path, capsys):
    path = _folder(tmp_path, inheritable=False) / "update.key"
    path.write_text("x", encoding="ascii")
    sign_release.restrict_to_owner(path)
    assert capsys.readouterr().err == ""


def test_pubkey_matches_keygen_output(tmp_path, capsys):
    path = tmp_path / "update.key"
    sign_release.main(["keygen", "--out", str(path)])
    printed = capsys.readouterr().out.strip()
    assert sign_release.main(["pubkey", "--key", str(path)]) == 0
    assert capsys.readouterr().out.strip() == printed


def test_pubkey_rejects_a_file_that_is_not_a_key(tmp_path, capsys):
    path = tmp_path / "junk.key"
    path.write_text("not hex", encoding="ascii")
    assert sign_release.main(["pubkey", "--key", str(path)]) == 1
    assert "not a key file" in capsys.readouterr().err


def test_sign_then_verify_roundtrip(exe, key_file, capsys):
    assert run_sign(exe, key_file) == 0
    sig = exe.parent / "MourseDecoder.exe.sig"
    output = capsys.readouterr().out
    digest = hashlib.sha256(EXE_BYTES).hexdigest()
    pubkey = signing.public_key(seed_of(key_file))
    assert TAG in output and digest in output and pubkey.hex() in output
    assert run_verify(exe, sig, pubkey=pubkey) == 0
    assert "OK" in capsys.readouterr().out


def test_signature_file_signs_the_expected_manifest(exe, key_file, tmp_path):
    out = tmp_path / "custom.sig"
    assert run_sign(exe, key_file, TAG, "--out", str(out)) == 0
    text = out.read_text(encoding="ascii")
    seed = seed_of(key_file)
    manifest = signing.build_manifest(TAG, "MourseDecoder.exe", hashlib.sha256(EXE_BYTES).hexdigest())
    # the asset name is fixed even though the local file is called downloaded-build.exe
    assert text == signing.encode_signature(signing.sign(seed, manifest))
    assert signing.verify(signing.public_key(seed), signing.parse_signature(text), manifest)


def test_tampered_exe_fails_verify(exe, key_file, capsys):
    run_sign(exe, key_file)
    sig = exe.parent / "MourseDecoder.exe.sig"
    pubkey = signing.public_key(seed_of(key_file))
    exe.write_bytes(EXE_BYTES + b"x")
    capsys.readouterr()
    assert run_verify(exe, sig, pubkey=pubkey) == 1
    assert "FAILED" in capsys.readouterr().err


def test_wrong_tag_fails_verify(exe, key_file):
    run_sign(exe, key_file)
    sig = exe.parent / "MourseDecoder.exe.sig"
    pubkey = signing.public_key(seed_of(key_file))
    assert run_verify(exe, sig, tag="v.0.1.10", pubkey=pubkey) == 1


def test_other_key_fails_verify(exe, key_file):
    run_sign(exe, key_file)
    sig = exe.parent / "MourseDecoder.exe.sig"
    assert run_verify(exe, sig, pubkey=signing.public_key(signing.generate_seed())) == 1


def test_verify_uses_pinned_keys_by_default(exe, key_file, monkeypatch):
    run_sign(exe, key_file)
    sig = exe.parent / "MourseDecoder.exe.sig"
    pubkey = signing.public_key(seed_of(key_file))
    args = ["verify", "--tag", TAG, "--exe", str(exe), "--sig", str(sig), "--no-git-check"]
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", ())
    assert sign_release.main(args) == 1
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (pubkey,))
    assert sign_release.main(args) == 0


def test_sign_refuses_a_key_that_is_not_pinned(exe, key_file, monkeypatch, capsys):
    other = signing.public_key(signing.generate_seed())
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (other,))
    assert sign_release.main(sign_args(exe, key_file, TAG, "--no-git-check")) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ") and "not in PINNED_PUBLIC_KEYS" in err and "--allow-unpinned" in err
    assert not (exe.parent / "MourseDecoder.exe.sig").exists()


def test_sign_refuses_when_nothing_is_pinned(exe, key_file, monkeypatch):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", ())
    assert sign_release.main(sign_args(exe, key_file, TAG, "--no-git-check")) == 1
    assert not (exe.parent / "MourseDecoder.exe.sig").exists()


def test_sign_allows_an_unpinned_key_with_the_flag_and_says_so(exe, key_file, monkeypatch, capsys):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (signing.public_key(signing.generate_seed()),))
    assert sign_release.main(sign_args(exe, key_file, TAG, "--no-git-check", "--allow-unpinned")) == 0
    assert "NOT pinned" in capsys.readouterr().out
    assert (exe.parent / "MourseDecoder.exe.sig").exists()


def test_sign_with_the_pinned_key_needs_no_flag_and_says_so(exe, key_file, monkeypatch, capsys):
    monkeypatch.setattr(signing, "PINNED_PUBLIC_KEYS", (signing.public_key(seed_of(key_file)),))
    assert sign_release.main(sign_args(exe, key_file, TAG, "--no-git-check")) == 0
    output = capsys.readouterr().out
    assert "(pinned)" in output and "NOT pinned" not in output


@pytest.mark.parametrize("tag", ["0.1.9", "v1", "v.0.1", "v.0.1.9-rc1", "v..0.1.9", "main", "", "v.0.1.9\n", "v.0.1.9 "])
def test_bad_tag_format_is_rejected(tag, exe, key_file, capsys):
    assert run_sign(exe, key_file, tag) == 1
    assert "Bad tag" in capsys.readouterr().err
    assert not (exe.parent / "MourseDecoder.exe.sig").exists()


def test_verify_rejects_a_tag_with_a_trailing_newline(exe, key_file, capsys):
    run_sign(exe, key_file)
    sig = exe.parent / "MourseDecoder.exe.sig"
    pubkey = signing.public_key(seed_of(key_file))
    capsys.readouterr()
    assert run_verify(exe, sig, tag=TAG + "\n", pubkey=pubkey) == 1
    assert "Bad tag" in capsys.readouterr().err


def test_a_value_error_from_the_signing_module_is_an_error_line_not_a_traceback(exe, key_file, monkeypatch, capsys):
    def refuse(*args):
        raise ValueError("no line breaks please")

    monkeypatch.setattr(signing, "build_manifest", refuse)
    assert run_sign(exe, key_file) == 1
    assert capsys.readouterr().err == "Error: no line breaks please\n"


@pytest.mark.parametrize("tag", ["v.0.1.9", "v0.1.9"])
def test_tag_forms_accepted_by_release_workflow(tag):
    assert sign_release.check_tag(tag) == tag


def test_missing_exe_is_a_clean_error(tmp_path, key_file, capsys):
    assert run_sign(tmp_path / "nope.exe", key_file) == 1
    assert "Cannot read" in capsys.readouterr().err


def test_sign_with_the_existing_tag_passes_the_git_check(exe, key_file, git_repo, capsys):
    assert sign_release.main(sign_args(exe, key_file, TAG, "--allow-unpinned")) == 0
    assert "Warning" not in capsys.readouterr().err


def test_sign_with_an_annotated_tag_passes_the_git_check(exe, key_file, git_repo, capsys):
    git_repo("tag", "-a", "v.0.2.0", "-m", "annotated")
    assert sign_release.main(sign_args(exe, key_file, "v.0.2.0", "--allow-unpinned")) == 0
    assert "Warning" not in capsys.readouterr().err


def test_sign_refuses_a_tag_spelling_that_does_not_exist_locally(exe, key_file, git_repo, capsys):
    # v0.1.9 passes the format check, but the pushed tag is v.0.1.9: clients would reject that signature
    assert sign_release.main(sign_args(exe, key_file, "v0.1.9", "--allow-unpinned")) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ") and "No local git tag 'v0.1.9'" in err
    assert not (exe.parent / "MourseDecoder.exe.sig").exists()


def test_verify_refuses_a_tag_spelling_that_does_not_exist_locally(exe, key_file, git_repo, capsys):
    assert sign_release.main(sign_args(exe, key_file, TAG, "--allow-unpinned")) == 0
    sig = exe.parent / "MourseDecoder.exe.sig"
    pubkey = signing.public_key(seed_of(key_file))
    capsys.readouterr()
    args = ["verify", "--exe", str(exe), "--sig", str(sig), "--pubkey", pubkey.hex()]
    assert sign_release.main([*args, "--tag", TAG]) == 0
    assert sign_release.main([*args, "--tag", "v0.1.9"]) == 1
    assert "No local git tag" in capsys.readouterr().err


def test_sign_warns_when_head_is_not_the_tagged_commit(exe, key_file, git_repo, capsys):
    git_repo("commit", "--allow-empty", "-q", "-m", "two")
    assert sign_release.main(sign_args(exe, key_file, TAG, "--allow-unpinned")) == 0
    assert "is not the commit of v.0.1.9" in capsys.readouterr().err


def test_the_git_check_can_be_skipped(exe, key_file, tmp_path, monkeypatch, capsys):
    plain = tmp_path / "not-a-repository"
    plain.mkdir()
    monkeypatch.setattr(sign_release, "ROOT", plain)
    if shutil.which("git") is not None:
        assert sign_release.main(sign_args(exe, key_file, TAG, "--allow-unpinned")) == 1
        assert "--no-git-check" in capsys.readouterr().err
    assert sign_release.main(sign_args(exe, key_file, TAG, "--allow-unpinned", "--no-git-check")) == 0


def test_missing_git_is_reported_and_skippable(exe, key_file, monkeypatch, capsys):
    real_run = subprocess.run

    def no_git(cmd, **kwargs):
        if cmd[0] == "git":
            raise FileNotFoundError(2, "No such file or directory")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(sign_release.subprocess, "run", no_git)
    assert sign_release.main(sign_args(exe, key_file, TAG, "--allow-unpinned")) == 1
    err = capsys.readouterr().err
    assert "Cannot run git" in err and "--no-git-check" in err
    assert sign_release.main(sign_args(exe, key_file, TAG, "--allow-unpinned", "--no-git-check")) == 0
