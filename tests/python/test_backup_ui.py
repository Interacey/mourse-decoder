"""Backup buttons on the settings page, with fake dialogs (skipped without a display)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mourse_decoder import backup
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.i18n import set_language
from mourse_decoder.settings import Settings
from test_backup import SQLITE, make_config, snapshot


class NoCapture:
    def begin_capture(self) -> None: ...
    def cancel_capture(self) -> None: ...
    def poll_capture(self) -> str | None:
        return None


class FakeDialogs:
    def __init__(self, save: str = "", open_: str = "", answer: bool = True) -> None:
        self.save, self.open_, self.answer = save, open_, answer
        self.calls: list[tuple[str, str, str]] = []
        self.initialfile = ""

    def save_path(self, title: str, initialfile: str) -> str:
        self.initialfile = initialfile
        return self.save

    def open_path(self, title: str) -> str:
        return self.open_

    def ask(self, title: str, text: str) -> bool:
        self.calls.append(("ask", title, text))
        return self.answer

    def info(self, title: str, text: str) -> None:
        self.calls.append(("info", title, text))

    def error(self, title: str, text: str) -> None:
        self.calls.append(("error", title, text))

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def make_panel(tk_root, fake_engine, injector, tmp_path):
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    pages = []

    def make(dialogs: FakeDialogs) -> SettingsPanel:
        config = make_config(tmp_path / "cfg" / "MourseDecoder")
        controller = MorseController(fake_engine, injector, DictionaryManager(), Settings())
        controller.start()
        page = SettingsPanel(
            tk_root, controller, DictionaryManager(), controller.apply_settings,
            capture_backend=NoCapture(), backup_dialogs=dialogs, config_dir=config,
        )
        page.pack()
        tk_root.update()
        page.config = config
        pages.append(page)
        return page

    yield make
    for page in pages:
        page.destroy()


def test_export_writes_the_zip_with_the_default_name_and_warns_about_secrets(make_panel, tmp_path):
    target = tmp_path / "out.zip"
    dialogs = FakeDialogs(save=str(target))
    page = make_panel(dialogs)
    page._export_backup()
    assert dialogs.initialfile.startswith("mourse-backup-") and dialogs.initialfile.endswith(".zip")
    assert len(dialogs.initialfile) == len("mourse-backup-YYYYMMDD.zip")
    assert dialogs.kinds() == ["info"]  # no telecode file, so no question
    assert "secrets" in dialogs.calls[0][2] and str(target) in dialogs.calls[0][2]
    assert backup.inspect_bundle(target, tmp_path / "empty").count("add") == 5


def test_export_asks_about_telecode_only_when_the_file_exists(make_panel, tmp_path):
    dialogs = FakeDialogs(save=str(tmp_path / "out.zip"), answer=True)
    page = make_panel(dialogs)
    (page.config / "telecode.sqlite").write_bytes(SQLITE)
    page._export_backup()
    assert dialogs.kinds() == ["ask", "info"]
    names = [e.path for e in backup.inspect_bundle(tmp_path / "out.zip", tmp_path / "e").entries]
    assert "telecode.sqlite" in names
    declined = FakeDialogs(save=str(tmp_path / "out2.zip"), answer=False)
    page._backup_dialogs = declined
    page._export_backup()
    names = [e.path for e in backup.inspect_bundle(tmp_path / "out2.zip", tmp_path / "e").entries]
    assert "telecode.sqlite" not in names


def test_cancelled_dialogs_do_nothing(make_panel, tmp_path):
    dialogs = FakeDialogs()
    page = make_panel(dialogs)
    before = snapshot(page.config)
    page._export_backup()
    page._import_backup()
    assert dialogs.calls == []
    assert snapshot(page.config) == before


def test_export_error_is_shown(make_panel, tmp_path):
    dialogs = FakeDialogs(save=str(tmp_path / "no" / "such" / "dir" / "out.zip"))
    page = make_panel(dialogs)
    page._export_backup()
    assert dialogs.kinds() == ["error"]
    assert dialogs.calls[0][1] == "Export failed"


def make_source(tmp_path: Path, text: str) -> Path:
    src = make_config(tmp_path / "src")
    (src / "settings.json").write_text(text)
    backup.export_bundle(src, tmp_path / "in.zip")
    return tmp_path / "in.zip"


def test_import_shows_a_preview_then_writes_and_names_the_restore_point(make_panel, tmp_path):
    zip_path = make_source(tmp_path, '{"alphabet": "fr"}')
    dialogs = FakeDialogs(open_=str(zip_path))
    page = make_panel(dialogs)
    page._import_backup()
    assert dialogs.kinds() == ["ask", "info"]
    confirm = dialogs.calls[0][2]
    assert "Replace: settings.json" in confirm and "replaced: 1" in confirm and "unchanged: 4" in confirm
    assert (page.config / "settings.json").read_text() == '{"alphabet": "fr"}'
    done = dialogs.calls[1][2]
    restore = next(page.config.parent.glob("MourseDecoder-backup-*.zip"))
    assert str(restore) in done and "Restart" in done


def test_declined_import_writes_nothing(make_panel, tmp_path):
    zip_path = make_source(tmp_path, '{"alphabet": "fr"}')
    dialogs = FakeDialogs(open_=str(zip_path), answer=False)
    page = make_panel(dialogs)
    before = snapshot(page.config)
    page._import_backup()
    assert dialogs.kinds() == ["ask"]
    assert snapshot(page.config) == before
    assert not list(page.config.parent.glob("*backup*"))


def test_import_of_an_identical_backup_says_nothing_to_do(make_panel, tmp_path):
    zip_path = tmp_path / "same.zip"
    dialogs = FakeDialogs(open_=str(zip_path))
    page = make_panel(dialogs)
    backup.export_bundle(page.config, zip_path)
    page._import_backup()
    assert dialogs.kinds() == ["info"] and "Nothing to do" in dialogs.calls[0][2]


def test_a_rejected_bundle_shows_the_error_and_changes_nothing(make_panel, tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"nope")
    dialogs = FakeDialogs(open_=str(bad))
    page = make_panel(dialogs)
    before = snapshot(page.config)
    page._import_backup()
    assert dialogs.kinds() == ["error"] and "not a readable zip" in dialogs.calls[0][2]
    assert snapshot(page.config) == before


def test_long_file_lists_are_shortened(make_panel, tmp_path):
    src = make_config(tmp_path / "src")
    for i in range(20):
        (src / "alphabets" / f"a{i:02}.json").write_text(json.dumps({"id": i}))
    backup.export_bundle(src, tmp_path / "in.zip")
    dialogs = FakeDialogs(open_=str(tmp_path / "in.zip"), answer=False)
    page = make_panel(dialogs)
    (page.config / "alphabets" / "mein.json").write_text("{}")
    page._import_backup()
    assert "… and 9 more" in dialogs.calls[0][2]
