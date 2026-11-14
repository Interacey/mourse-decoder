"""Tests for packaging/build_msi.py (the WiX call itself is not run here)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from mourse_decoder import __version__

SCRIPT = Path(__file__).resolve().parents[2] / "packaging" / "build_msi.py"
_spec = importlib.util.spec_from_file_location("build_msi", SCRIPT)
build_msi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_msi)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.1.10", "0.1.10"),
        ("v.0.1.10", "0.1.10"),
        ("v0.1.10", "0.1.10"),
        ("0.0.0", "0.0.0"),
        ("255.255.65535", "255.255.65535"),
        ("0.1.010", "0.1.10"),
    ],
)
def test_msi_version_accepts_three_numeric_fields(text, expected):
    assert build_msi.msi_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0.1",
        "1",
        "0.1.10.1",
        "0.1.x",
        "0.1.10-rc1",
        "0.1.10+5",
        "-1.0.0",
        "256.0.0",
        "0.256.0",
        "0.0.65536",
        "xv.0.1.10",
        "vv0.1.10",
        "v..0.1.10",
        "V.0.1.10",
        "version 0.1.10",
        " 0.1.10",
        "0.1.10 ",
        "0.1.10\n",
        "0..10",
        "\u0660.\u0661.\u0662",  # Arabic-Indic digits
    ],
)
def test_msi_version_rejects_everything_else(text):
    with pytest.raises(ValueError):
        build_msi.msi_version(text)


def test_package_version_is_read_from_init_without_importing_it():
    assert build_msi.package_version() == __version__
    assert build_msi.msi_version(build_msi.package_version())


def test_wix_command_passes_version_exe_icon_and_output():
    exe, out = Path("C:/x/MourseDecoder.exe"), Path("C:/x/MourseDecoder.msi")
    assert build_msi.wix_command("wix", exe, "0.1.10", out) == [
        "wix", "build", str(build_msi.WXS),
        "-arch", "x64",
        "-d", "Version=0.1.10",
        "-d", f"ExePath={exe}",
        "-d", f"IconPath={build_msi.ICON}",
        "-o", str(out),
    ]


def test_default_paths_point_into_the_repository():
    assert build_msi.WXS.is_file()
    assert build_msi.INIT.is_file()


@pytest.fixture
def exe(tmp_path, monkeypatch):
    path = tmp_path / "MourseDecoder.exe"
    path.write_bytes(b"MZ")
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    monkeypatch.setattr(build_msi, "ICON", icon)
    return path


@pytest.fixture
def calls(monkeypatch):
    """wix is 'found' and every subprocess call is recorded and succeeds."""
    recorded = []
    monkeypatch.setattr(build_msi.shutil, "which", lambda name: "wix-path")
    monkeypatch.setattr(build_msi.subprocess, "call", lambda command, **kwargs: recorded.append(command) or 0)
    return recorded


def test_main_builds_then_validates(exe, calls, tmp_path):
    out = tmp_path / "sub" / "MourseDecoder.msi"
    assert build_msi.main(["--exe", str(exe), "--version", "v.0.1.10", "--output", str(out)]) == 0
    build, validate = calls
    assert build[:2] == ["wix-path", "build"] and "Version=0.1.10" in build
    assert validate == ["wix-path", "msi", "validate", str(out)]
    assert out.parent.is_dir()


def test_main_uses_the_package_version_by_default(exe, calls, tmp_path):
    assert build_msi.main(["--exe", str(exe), "--output", str(tmp_path / "a.msi")]) == 0
    assert f"Version={build_msi.msi_version(__version__)}" in calls[0]


def test_main_stops_when_the_build_fails(exe, calls, monkeypatch, tmp_path):
    monkeypatch.setattr(build_msi.subprocess, "call", lambda command, **kwargs: calls.append(command) or 7)
    assert build_msi.main(["--exe", str(exe), "--output", str(tmp_path / "a.msi")]) == 7
    assert len(calls) == 1


def test_main_returns_the_validation_exit_code(exe, calls, monkeypatch, tmp_path):
    monkeypatch.setattr(build_msi.subprocess, "call", lambda command, **kwargs: 204 if command[1] == "msi" else 0)
    assert build_msi.main(["--exe", str(exe), "--output", str(tmp_path / "a.msi")]) == 204


def test_main_reports_a_missing_wix_with_the_pinned_install_command(exe, calls, monkeypatch, capsys):
    monkeypatch.setattr(build_msi.shutil, "which", lambda name: None)
    assert build_msi.main(["--exe", str(exe)]) == 1
    assert "dotnet tool install --global wix --version 5.0.2" in capsys.readouterr().err
    assert calls == []


def test_main_rejects_a_bad_version_before_anything_runs(exe, calls, capsys):
    assert build_msi.main(["--exe", str(exe), "--version", "0.1"]) == 1
    assert "Bad version" in capsys.readouterr().err
    assert calls == []


def test_main_reports_a_missing_exe(calls, tmp_path, capsys):
    assert build_msi.main(["--exe", str(tmp_path / "nope.exe")]) == 1
    assert "not found" in capsys.readouterr().err
    assert calls == []


def test_main_reports_a_missing_icon(exe, calls, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(build_msi, "ICON", tmp_path / "missing.ico")
    assert build_msi.main(["--exe", str(exe)]) == 1
    assert "icon" in capsys.readouterr().err
    assert calls == []


WIX_NS = "{http://wixtoolset.org/schemas/v4/wxs}"


def test_the_registry_values_the_updater_reads_are_the_ones_the_installer_writes():
    from xml.etree import ElementTree

    from mourse_decoder import updater

    root = ElementTree.parse(build_msi.WXS).getroot()
    values = {
        (v.get("Root"), v.get("Key"), v.get("Name")): v.get("Value")
        for v in root.iter(f"{WIX_NS}RegistryValue")
    }
    # install_kind() reads InstallDir and uninstall() reads ProductCode, both from HKCU\<REG_KEY>
    assert values[("HKCU", updater.REG_KEY, "InstallDir")] == "[INSTALLFOLDER]"
    assert values[("HKCU", updater.REG_KEY, "ProductCode")] == "[ProductCode]"
