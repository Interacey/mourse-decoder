"""Backup buttons on the settings page, with fake dialogs (skipped without a display)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from mourse_decoder import backup
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.function_keys import FunctionKeysStore
from mourse_decoder.i18n import set_language
from mourse_decoder.settings import Settings, SettingsStore
from test_backup import SQLITE, make_config, snapshot


class NoCapture:
    def begin_capture(self) -> None: ...
    def cancel_capture(self) -> None: ...
    def poll_capture(self) -> str | None:
        return None


class FakeDialogs:
    def __init__(self, save: str = "", open_: str = "", answer: bool = True, choice: bool | None = True) -> None:
        self.save, self.open_, self.answer, self.choice = save, open_, answer, choice
        self.calls: list[tuple[str, str, str]] = []
        self.initialfile = ""
        self.on_ask = None  # runs while the confirmation is open (a test changes the zip there)

    def save_path(self, title: str, initialfile: str) -> str:
        self.initialfile = initialfile
        return self.save

    def open_path(self, title: str) -> str:
        return self.open_

    def ask(self, title: str, text: str) -> bool:
        self.calls.append(("ask", title, text))
        if self.on_ask:
            self.on_ask()
        return self.answer

    def choose(self, title: str, text: str) -> bool | None:
        self.calls.append(("choose", title, text))
        return self.choice

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
        store = SettingsStore(config)

        hooks: list = []  # what window.sync() does after the tray applied settings: refresh the pages

        def apply(settings: Settings) -> None:  # what the tray does: apply, save, sync
            controller.apply_settings(settings)
            store.save(settings)
            for hook in hooks:
                hook()

        page = SettingsPanel(
            tk_root, controller, DictionaryManager(), apply,
            capture_backend=NoCapture(), backup_dialogs=dialogs, config_dir=config,
            on_imported=lambda: [hook() for hook in hooks],
        )
        page.pack()
        tk_root.update()
        page.config, page.controller, page.root, page.hooks = config, controller, tk_root, hooks
        pages.append(page)
        return page

    yield make
    for page in pages:
        page.destroy()


def settle(page, seconds: float = 20) -> None:
    """Run the Tk loop until the page's backup job (a worker thread) is finished and its dialogs have run."""
    deadline = time.monotonic() + seconds
    while page._backup_running and time.monotonic() < deadline:
        page.root.update()
        time.sleep(0.005)
    page.root.update()
    assert not page._backup_running, "the backup job did not finish"


def test_export_writes_the_zip_with_the_default_name_and_warns_about_secrets(make_panel, tmp_path):
    target = tmp_path / "out.zip"
    dialogs = FakeDialogs(save=str(target))
    page = make_panel(dialogs)
    page._export_backup()
    settle(page)
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
    settle(page)
    assert dialogs.kinds() == ["ask", "info"]
    names = [e.path for e in backup.inspect_bundle(tmp_path / "out.zip", tmp_path / "e").entries]
    assert "telecode.sqlite" in names
    declined = FakeDialogs(save=str(tmp_path / "out2.zip"), answer=False)
    page._backup_dialogs = declined
    page._export_backup()
    settle(page)
    names = [e.path for e in backup.inspect_bundle(tmp_path / "out2.zip", tmp_path / "e").entries]
    assert "telecode.sqlite" not in names


def test_cancelled_dialogs_do_nothing(make_panel, tmp_path):
    dialogs = FakeDialogs()
    page = make_panel(dialogs)
    before = snapshot(page.config)
    page._export_backup()
    settle(page)
    page._import_backup()
    settle(page)
    assert dialogs.calls == []
    assert snapshot(page.config) == before


def test_export_error_is_shown(make_panel, tmp_path):
    dialogs = FakeDialogs(save=str(tmp_path / "no" / "such" / "dir" / "out.zip"))
    page = make_panel(dialogs)
    page._export_backup()
    settle(page)
    assert dialogs.kinds() == ["error"]
    assert dialogs.calls[0][1] == "Export failed"


def make_source(tmp_path: Path, text: str) -> Path:
    src = make_config(tmp_path / "src")
    (src / "settings.json").write_text(text)
    backup.export_bundle(src, tmp_path / "in.zip")
    return tmp_path / "in.zip"


def test_import_shows_a_preview_then_writes_and_names_the_restore_point(make_panel, tmp_path):
    zip_path = make_source(tmp_path, '{"alphabet": "greek"}')
    dialogs = FakeDialogs(open_=str(zip_path))
    page = make_panel(dialogs)
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["ask", "info"]
    confirm = dialogs.calls[0][2]
    assert "Replace: settings.json" in confirm and "replaced: 1" in confirm and "unchanged: 4" in confirm
    assert SettingsStore(page.config).load().alphabet == "greek"
    done = dialogs.calls[1][2]
    restore = next(page.config.parent.glob("MourseDecoder-backup-*.zip"))
    assert str(restore) in done and "active now" in done and "Restart" not in done


def test_declined_import_writes_nothing(make_panel, tmp_path):
    zip_path = make_source(tmp_path, '{"alphabet": "greek"}')
    dialogs = FakeDialogs(open_=str(zip_path), answer=False)
    page = make_panel(dialogs)
    before = snapshot(page.config)
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["ask"]
    assert snapshot(page.config) == before
    assert not list(page.config.parent.glob("*backup*"))


def test_import_of_an_identical_backup_says_nothing_to_do(make_panel, tmp_path):
    zip_path = tmp_path / "same.zip"
    dialogs = FakeDialogs(open_=str(zip_path))
    page = make_panel(dialogs)
    backup.export_bundle(page.config, zip_path)
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["info"] and "Nothing to do" in dialogs.calls[0][2]


def test_a_rejected_bundle_shows_the_error_and_changes_nothing(make_panel, tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"nope")
    dialogs = FakeDialogs(open_=str(bad))
    page = make_panel(dialogs)
    before = snapshot(page.config)
    page._import_backup()
    settle(page)
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
    settle(page)
    assert "… and 9 more" in dialogs.calls[0][2]


# the imported state reaches the running program

SLOT_13 = {"slot": 13, "trigger": "F13", "macro": "{Win+R}{WaitWindow Run}powershell -w hidden -c calc{Enter}"}


def make_keys_source(tmp_path: Path, settings: str = '{"alphabet": "greek", "trigger": "F8"}') -> Path:
    src = make_config(tmp_path / "src")
    (src / "settings.json").write_text(settings)
    (src / "function_keys.json").write_text(json.dumps({"schema_version": 1, "slots": [SLOT_13]}))
    backup.export_bundle(src, tmp_path / "keys.zip")
    return tmp_path / "keys.zip"


class FakeToast:
    def show(self, *_args, **_kwargs) -> None: ...


def function_keys_page(page):
    from mourse_decoder.ui.function_keys_panel import FunctionKeysPanel

    panel = FunctionKeysPanel(
        page.root, page.controller, FunctionKeysStore(page.config), capture_backend=NoCapture(), toast=FakeToast()
    )
    panel.pack()
    page.root.update()
    return panel


def test_after_an_import_later_saves_write_the_imported_state_not_the_old_one(make_panel, tmp_path, fake_engine):
    dialogs = FakeDialogs(open_=str(make_keys_source(tmp_path)), choice=True)
    page = make_panel(dialogs)
    panel = function_keys_page(page)
    page.hooks.append(panel.refresh)  # the main window's sync() refreshes every built page
    assert panel._keys.slot(13).macro == ""  # the panel loaded the old file
    try:
        page._import_backup()
        settle(page)
        assert dialogs.kinds()[-2:] == ["choose", "info"]
        assert page.controller.settings.alphabet == "greek" and page.controller.settings.trigger == "F8"
        assert page.controller.function_keys.slot(13).macro.startswith("{Win+R}")
        assert fake_engine.function_keys and fake_engine.function_keys[0][0] == 13  # installed in the hook
        assert "Restart" not in dialogs.calls[-1][2]

        # a change in the settings form saves the imported state plus that change
        page._notify.set(False)
        page.root.update()
        saved = SettingsStore(page.config).load()
        assert (saved.alphabet, saved.trigger, saved.notifications) == ("greek", "F8", False)
        # and so does a save in the function keys editor
        assert panel._keys.slot(13).macro.startswith("{Win+R}")
        assert panel._commit(panel._keys.replace_slot(panel._keys.slot(14).replace(macro="x", trigger="F14")))
        slots = {s["slot"] for s in json.loads((page.config / "function_keys.json").read_text())["slots"]}
        assert slots == {13, 14}
    finally:
        panel.destroy()


def test_the_function_keys_page_keeps_its_edits_while_the_file_is_unchanged(make_panel):
    page = make_panel(FakeDialogs())
    panel = function_keys_page(page)
    try:
        panel._macro.insert("1.0", "typed but not saved")
        panel.refresh()
        assert "typed but not saved" in panel._macro.get("1.0", "end")
    finally:
        panel.destroy()


def test_an_old_table_that_clashes_with_the_new_morse_key_does_not_stop_the_load(make_panel, tmp_path):
    page = make_panel(FakeDialogs())
    keys = FunctionKeysStore(page.config).load()[0]
    page.controller.apply_function_keys(keys.replace_slot(keys.slot(13).replace(macro="old", trigger="F13")))
    src = make_config(tmp_path / "src")
    (src / "settings.json").write_text('{"trigger": "F13"}')  # clashes with the old slot 13
    (src / "function_keys.json").write_text(
        json.dumps({"schema_version": 1, "slots": [{"slot": 14, "trigger": "F14", "macro": "new"}]})
    )
    backup.export_bundle(src, tmp_path / "pair.zip")
    dialogs = FakeDialogs(open_=str(tmp_path / "pair.zip"), choice=True)
    page._backup_dialogs = dialogs
    page._import_backup()
    settle(page)
    assert page.controller.settings.trigger == "F13"
    assert page.controller.function_keys.slot(14).macro == "new" and page.controller.function_keys.slot(13).macro == ""
    assert "could not load" not in dialogs.calls[-1][2]


def test_what_cannot_be_loaded_live_is_named_and_the_files_stay_imported(make_panel, tmp_path):
    src = make_config(tmp_path / "src")
    (src / "languages" / "xx.json").write_text('{"id": "xx", "name": "XX", "strings": {}}')
    (src / "settings.json").write_text('{"alphabet": "no-such-alphabet"}')
    backup.export_bundle(src, tmp_path / "in.zip")
    dialogs = FakeDialogs(open_=str(tmp_path / "in.zip"))
    page = make_panel(dialogs)
    (page.config / "languages" / "xx.json").write_text("{}")
    page._import_backup()
    settle(page)
    text = dialogs.calls[-1][2]
    assert "could not load them" in text and "language files" in text
    assert (page.config / "settings.json").read_text() == '{"alphabet": "no-such-alphabet"}'


# choices in the confirmation


def test_macros_in_a_bundle_get_a_warning_with_a_summary_and_three_choices(make_panel, tmp_path):
    dialogs = FakeDialogs(open_=str(make_keys_source(tmp_path)), choice=None)
    page = make_panel(dialogs)
    before = snapshot(page.config)
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["choose"]  # cancel: nothing happens
    text = dialogs.calls[0][2]
    assert "WARNING" in text and "F13 (F13): {Win+R}{WaitWindow Run}powershell" in text
    assert "[{Win+R}, {WaitWindow …}, {Enter}]" in text and "Commands that act outside the typed text" in text
    assert "Yes: import everything" in text and "No: import without the files that contain macros or commands" in text and "Cancel" in text
    assert snapshot(page.config) == before and not list(page.config.parent.glob("*backup*"))


def test_choosing_no_imports_everything_but_function_keys(make_panel, tmp_path):
    dialogs = FakeDialogs(open_=str(make_keys_source(tmp_path)), choice=False)
    page = make_panel(dialogs)
    own = (page.config / "function_keys.json").read_bytes()
    page._import_backup()
    settle(page)
    assert (page.config / "function_keys.json").read_bytes() == own
    assert page.controller.settings.trigger == "F8" and page.controller.function_keys.slot(13).macro == ""
    assert dialogs.kinds() == ["choose", "info"]


def test_a_bundle_without_macros_asks_the_plain_question(make_panel, tmp_path):
    dialogs = FakeDialogs(open_=str(make_source(tmp_path, '{"alphabet": "greek"}')))
    page = make_panel(dialogs)
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["ask", "info"] and "WARNING" not in dialogs.calls[0][2]


def test_unusable_function_keys_offer_the_rest_without_them(make_panel, tmp_path):
    import hashlib
    import zipfile

    src = make_config(tmp_path / "src")
    (src / "settings.json").write_text('{"alphabet": "greek"}')
    backup.export_bundle(src, tmp_path / "in.zip")
    broken = tmp_path / "broken.zip"
    data = b'{"slots": [1, 2'
    with zipfile.ZipFile(tmp_path / "in.zip") as old, zipfile.ZipFile(broken, "w") as new:
        manifest = json.loads(old.read("manifest.json"))
        for entry in manifest["files"]:
            if entry["path"] == "function_keys.json":
                entry["size"], entry["sha256"] = len(data), hashlib.sha256(data).hexdigest()
        new.writestr("manifest.json", json.dumps(manifest))
        for name in old.namelist():
            if name != "manifest.json":
                new.writestr(name, data if name == "function_keys.json" else old.read(name))
    dialogs = FakeDialogs(open_=str(broken), answer=True)
    page = make_panel(dialogs)
    own = (page.config / "function_keys.json").read_bytes()
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["ask", "ask", "info"]
    assert "function_keys.json is not usable" in dialogs.calls[0][2] and "without it" in dialogs.calls[0][2]
    assert (page.config / "function_keys.json").read_bytes() == own
    assert page.controller.settings.alphabet == "greek"


def test_a_zip_that_changes_while_the_confirmation_is_open_is_not_imported(make_panel, tmp_path):
    zip_path = make_source(tmp_path, '{"alphabet": "greek"}')
    dialogs = FakeDialogs(open_=str(zip_path))
    page = make_panel(dialogs)
    before = snapshot(page.config)

    def swap() -> None:
        other = make_source(tmp_path / "other", '{"alphabet": "cyrillic"}')
        zip_path.write_bytes(other.read_bytes())

    (tmp_path / "other").mkdir()
    dialogs.on_ask = swap
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["ask", "error"] and "changed after it was checked" in dialogs.calls[1][2]
    assert snapshot(page.config) == before


# errors are shown, never raised in the Tk callback


def test_a_restore_point_is_always_named_in_an_error(make_panel, tmp_path, monkeypatch):
    zip_path = make_source(tmp_path, '{"alphabet": "greek"}')
    point = tmp_path / "somewhere" / "RP.zip"

    def failing(*_a, **_k):
        raise backup.BackupError("Writing x failed", restore_point=point)

    monkeypatch.setattr(backup, "import_bundle", failing)
    dialogs = FakeDialogs(open_=str(zip_path))
    page = make_panel(dialogs)
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["ask", "error"]
    assert "Writing x failed" in dialogs.calls[1][2] and str(point) in dialogs.calls[1][2]


@pytest.mark.parametrize("where", ["inspect", "import", "export", "dialog"])
def test_unexpected_exceptions_become_dialogs(make_panel, tmp_path, monkeypatch, where):
    zip_path = make_source(tmp_path, '{"alphabet": "greek"}')

    def boom(*_a, **_k):
        raise ZeroDivisionError("surprise")

    dialogs = FakeDialogs(open_=str(zip_path), save=str(tmp_path / "o.zip"))
    page = make_panel(dialogs)
    action = page._export_backup if where == "export" else page._import_backup
    if where == "dialog":
        monkeypatch.setattr(dialogs, "open_path", boom)
    else:
        monkeypatch.setattr(backup, {"inspect": "inspect_bundle", "import": "import_bundle", "export": "export_bundle"}[where], boom)
    action()
    settle(page)
    errors = [c for c in dialogs.calls if c[0] == "error"]
    assert len(errors) == 1 and "ZeroDivisionError" in errors[0][2]
    assert page._backup_running is False and all(b._enabled for b in page._backup_buttons)


# the worker thread


def test_the_job_runs_in_a_thread_and_its_result_arrives_on_the_tk_thread(make_panel):
    dialogs = FakeDialogs()
    page = make_panel(dialogs)
    main = threading.get_ident()
    seen: dict = {}
    release = threading.Event()

    def work():
        seen["worker"] = threading.get_ident()
        release.wait(5)
        return 42

    def done(result, error):
        seen.update(thread=threading.get_ident(), result=result, error=error)
        page._finish()

    page._start(work, done)
    assert page._backup_running and not any(b._enabled for b in page._backup_buttons)
    page.root.update()
    assert "thread" not in seen  # still waiting: the Tk loop is not blocked, and done did not run
    page._import_backup()  # a second click while it runs does nothing
    page._export_backup()
    assert dialogs.calls == []
    release.set()
    settle(page)
    assert seen["worker"] != main and seen["thread"] == main
    assert (seen["result"], seen["error"]) == (42, None)
    assert all(b._enabled for b in page._backup_buttons)


def test_settings_changes_are_held_back_while_a_job_runs(make_panel):
    page = make_panel(FakeDialogs())
    release = threading.Event()
    page._start(lambda: release.wait(5), lambda *_: page._finish())
    page._notify.set(False)
    page.root.update()
    assert page.controller.settings.notifications is True  # not applied during the job
    release.set()
    settle(page)


def test_a_job_that_ends_after_the_page_is_gone_touches_nothing(make_panel):
    page = make_panel(FakeDialogs())
    release = threading.Event()
    calls: list = []
    page._start(lambda: release.wait(5), lambda *a: calls.append(a))
    page.destroy()
    release.set()
    time.sleep(0.1)
    page.root.update()
    assert calls == []


# nothing else saves while an import runs

class FakeTray:
    """What TrayApp._apply and the poll loop of TrayApp need, with the real methods run on it."""

    def __init__(self, controller, config):
        from unittest.mock import MagicMock

        self._controller, self._store = controller, SettingsStore(config)
        self._icon, self._window = MagicMock(), MagicMock()
        self._set_title, self._tooltip = MagicMock(), MagicMock(return_value="t")
        self.polled: list[str] = []
        self._ui_queue = __import__("queue").Queue()
        self._root = MagicMock()
        self._quit_pending = False
        self._saved_flags = {"enabled": controller.settings.enabled, "block_input": controller.settings.block_input}
        self._drain_queue = lambda: None  # the loop schedules itself again with this
        for name in ("block", "cycle", "undo", "function_keys", "hook", "calibration"):
            setattr(self, f"_poll_{name}", self._recorder(name))

    def _recorder(self, name):
        def poll():
            self.polled.append(name)

        poll.__name__ = f"_poll_{name}"
        return poll

    def _to_save(self, settings, runtime_only, chosen=()):
        from mourse_decoder.ui.tray import TrayApp

        return TrayApp._to_save(self, settings, runtime_only, chosen)

    def apply(self, settings):
        from mourse_decoder.ui.tray import TrayApp

        TrayApp._apply(self, settings)

    def drain(self):
        from mourse_decoder.ui.tray import TrayApp

        TrayApp._drain_queue(self)


@pytest.fixture(autouse=True)
def gate_is_free():
    from mourse_decoder.import_gate import gate

    gate.reset()
    yield
    gate.reset()


def test_the_tray_refuses_to_save_while_an_import_runs_and_saves_the_imported_state_after(make_panel, tmp_path):
    from mourse_decoder.import_gate import ImportBusy, gate

    release, inside = threading.Event(), threading.Event()
    real = backup.import_bundle

    def slow_import(*args, **kwargs):
        inside.set()
        release.wait(10)
        return real(*args, **kwargs)

    dialogs = FakeDialogs(open_=str(make_keys_source(tmp_path)), choice=True)
    page = make_panel(dialogs)
    tray = FakeTray(page.controller, page.config)
    page.hooks.append(lambda: None)
    import mourse_decoder.backup as module

    original, module.import_bundle = module.import_bundle, slow_import
    try:
        page._import_backup()
        deadline = time.monotonic() + 20
        while not inside.is_set() and time.monotonic() < deadline:
            page.root.update()
            time.sleep(0.005)
        assert inside.is_set() and gate.blocked()
        before = page.controller.settings
        with pytest.raises(ImportBusy):
            tray.apply(before.replace(notifications=False))  # a tray menu change: refused, nothing saved
        assert page.controller.settings == before
        assert SettingsStore(page.config).load().notifications is True
        tray.drain()  # the shortcut polls wait, the others run
        assert "block" not in tray.polled and "cycle" not in tray.polled and "function_keys" in tray.polled
        release.set()
        settle(page)
    finally:
        module.import_bundle = original
        release.set()
    assert not gate.blocked()
    tray.polled.clear()
    tray.drain()
    assert "block" in tray.polled and "cycle" in tray.polled  # pending presses are looked at now
    # after the import a tray change writes the imported state, not the old one
    tray.apply(page.controller.settings.replace(notifications=False))
    saved = SettingsStore(page.config).load()
    assert (saved.alphabet, saved.trigger, saved.notifications) == ("greek", "F8", False)


@pytest.mark.parametrize("how", ["error", "cancel_confirm", "success", "closed"])
def test_the_gate_is_released_whatever_happens(make_panel, tmp_path, monkeypatch, how):
    from mourse_decoder.import_gate import gate

    seen: list[bool] = []
    real = backup.import_bundle

    def spying(*args, **kwargs):
        seen.append(gate.blocked())
        if how == "error":
            raise RuntimeError("boom")
        if how == "closed":
            time.sleep(0.2)
        return real(*args, **kwargs)

    monkeypatch.setattr(backup, "import_bundle", spying)
    dialogs = FakeDialogs(open_=str(make_source(tmp_path, '{"alphabet": "greek"}')), answer=how != "cancel_confirm")
    page = make_panel(dialogs)
    page._import_backup()
    if how == "closed":
        deadline = time.monotonic() + 20
        while not gate.blocked() and time.monotonic() < deadline:
            page.root.update()
            time.sleep(0.005)
        assert gate.blocked()
        page.destroy()
        assert gate.blocked()  # the worker is still writing: the gate stays until it is done
        deadline = time.monotonic() + 10
        while gate.blocked() and time.monotonic() < deadline:
            time.sleep(0.01)
    else:
        settle(page)
    assert not gate.blocked()
    assert seen in ([True], []) and (how == "cancel_confirm") == (seen == [])


def test_the_gate_allows_the_import_itself_and_nests():
    from mourse_decoder.import_gate import ImportBusy, gate

    assert gate.begin() and not gate.begin()
    with pytest.raises(ImportBusy):
        gate.check()
    with gate.allow():
        gate.check()
        with gate.allow():
            gate.check()
        gate.check()
    assert gate.blocked()
    try:
        with gate.allow():
            raise ValueError("inside")
    except ValueError:
        pass
    assert gate.blocked()  # the permission is gone again after an error
    gate.end()
    gate.check()


def test_the_function_keys_editor_does_not_save_while_an_import_runs(make_panel):
    from mourse_decoder.import_gate import gate

    page = make_panel(FakeDialogs())
    panel = function_keys_page(page)
    try:
        before = (page.config / "function_keys.json").read_bytes()
        new = panel._keys.replace_slot(panel._keys.slot(14).replace(macro="x", trigger="F14"))
        assert gate.begin()
        assert panel._commit(new) is False
        assert "import is running" in panel._error.cget("text")
        assert (page.config / "function_keys.json").read_bytes() == before
        gate.end()
        assert panel._commit(new) is True  # the edit was not lost: saving again works
    finally:
        panel.destroy()


def test_errors_are_shown_in_the_current_language(make_panel):
    from mourse_decoder.i18n import set_language

    page = make_panel(FakeDialogs())
    error = backup.BackupError("checksum", name="settings.json")
    try:
        assert page._error_text(error) == str(error)
        set_language("de")
        assert page._error_text(error) == "settings.json passt nicht zur Prüfsumme; das Zip ist beschädigt oder wurde verändert."
        with_point = backup.BackupError("write_failed", name="a", error="e", restore_point=Path("/x/RP.zip"))
        assert "RP.zip" in page._error_text(with_point)
        assert page._error_text(backup.BackupError("free text, no code")) == "free text, no code"
    finally:
        set_language("en")


# review round 2


def alpha_bytes(alphabet_id: str, codes: dict | None = None) -> bytes:
    return json.dumps({"id": alphabet_id, "name": alphabet_id, "codes": codes or {".-": "a"}}).encode()


def zip_of(tmp_path: Path, files: dict[str, bytes], name: str = "r2.zip") -> Path:
    from test_backup import build_zip

    return build_zip(tmp_path / name, files)


def test_commands_in_alphabets_ask_the_three_way_question_and_no_leaves_them_out(make_panel, tmp_path):
    files = {
        "alphabets/evil.json": alpha_bytes("evil", {".-": "{Win+R}notepad{Enter}"}),
        "alphabets/plain.json": alpha_bytes("plain"),
    }
    dialogs = FakeDialogs(open_=str(zip_of(tmp_path, files)), choice=False)
    page = make_panel(dialogs)
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["choose", "info"]
    text = dialogs.calls[0][2]
    assert "WARNING" in text and "alphabets/evil.json [.-]: {Win+R}notepad{Enter} [{Win+R}, {Enter}]" in text
    assert (page.config / "alphabets" / "plain.json").exists() and not (page.config / "alphabets" / "evil.json").exists()


def test_the_choice_without_macros_shows_its_own_result_and_fails_before_anything_is_written(make_panel, tmp_path):
    files = {"settings.json": b'{"trigger": "F13"}', "function_keys.json": json.dumps(
        {"schema_version": 1, "slots": [{"slot": 14, "trigger": "F14", "macro": "{Win+R}"}]}).encode()}
    dialogs = FakeDialogs(open_=str(zip_of(tmp_path, files)), choice=False)
    page = make_panel(dialogs)
    (page.config / "function_keys.json").write_text(
        json.dumps({"schema_version": 1, "slots": [{"slot": 13, "trigger": "F13", "macro": "old"}]}))
    before = snapshot(page.config)
    page._import_backup()
    settle(page)
    assert dialogs.kinds() == ["choose", "error"]
    assert "Importing without those files is not possible" in dialogs.calls[0][2]  # said before the choice
    assert "would clash" in dialogs.calls[1][2]
    assert snapshot(page.config) == before and not list(page.config.parent.glob("*backup*"))


def test_all_macros_are_in_the_confirmation_risky_ones_first(make_panel, tmp_path):
    from mourse_decoder.function_keys import default_trigger

    slots = [{"slot": n, "trigger": f"F{n}", "macro": f"plain {n}"} for n in range(13, 25)]
    slots += [{"slot": n, "trigger": f"Ctrl+Alt+F{n}", "macro": f"plain {n}"} for n in range(1, 9)]
    slots.append({"slot": 40, "trigger": default_trigger(40), "macro": "{Win+R}"})
    files = {"function_keys.json": json.dumps({"schema_version": 1, "slots": slots}).encode()}
    dialogs = FakeDialogs(open_=str(zip_of(tmp_path, files)), choice=None)
    page = make_panel(dialogs)
    page._import_backup()
    settle(page)
    text = dialogs.calls[0][2]
    assert all(f"plain {n}" in text for n in [*range(1, 9), *range(13, 25)])  # nothing cut off
    assert text.index("F40") < text.index("plain 13")  # the risky one first


def test_the_dialog_scrolls_and_returns_the_choice(tk_root):
    import tkinter as tk

    from mourse_decoder.ui.settings_panel import BackupDialogs

    set_language("en")
    long_text = "\n".join(f"line {n}" for n in range(300))
    dialogs = BackupDialogs(tk_root)

    def press(index: int) -> None:
        top = next(w for w in tk_root.winfo_children() if isinstance(w, tk.Toplevel))
        frames = [w for w in top.winfo_children() if isinstance(w, tk.Frame)]
        text = next(w for f in frames for w in f.winfo_children() if isinstance(w, tk.Text))
        bar = next(w for f in frames for w in f.winfo_children() if isinstance(w, tk.Scrollbar))
        assert "line 299" in text.get("1.0", "end") and bar is not None  # everything is there, behind a scroll bar
        buttons = [w for f in frames for w in f.winfo_children() if isinstance(w, tk.Button)]
        buttons[index].invoke()

    for index, expected in ((0, True), (1, False), (2, None)):
        tk_root.after(300, press, index)
        assert dialogs.choose("t", long_text) is expected


def test_the_confirmation_does_not_depend_on_a_user_language_file(make_panel, tmp_path):
    from mourse_decoder import i18n

    languages = tmp_path / "userlang"
    languages.mkdir()
    # a language file that arrived with an earlier import and drops the warning and the list from the dialog
    (languages / "xx.json").write_text(json.dumps({"id": "xx", "name": "XX", "strings": {
        "backup.import.confirm": "Import?", "backup.import.warn.macros": "", "backup.import.slot": "",
        "backup.import.choice": ""}}), encoding="utf-8")
    files = {"function_keys.json": json.dumps(
        {"schema_version": 1, "slots": [{"slot": 13, "trigger": "F13", "macro": "{Win+R}"}]}).encode()}
    dialogs = FakeDialogs(open_=str(zip_of(tmp_path, files)), choice=None)
    page = make_panel(dialogs)
    try:
        i18n.set_language("xx", languages)
        assert i18n.current_language() == "xx" and i18n.tr("backup.import.confirm") == "Import?"
        page._import_backup()
        settle(page)
    finally:
        i18n.set_language("en", None)
    text = dialogs.calls[0][2]
    assert "WARNING" in text and "F13 (F13): {Win+R}" in text and "Yes: import everything" in text


def test_an_import_of_alphabets_alone_does_not_rewrite_settings_json(make_panel, tmp_path):
    page = make_panel(FakeDialogs(open_=str(zip_of(tmp_path, {"alphabets/neu.json": alpha_bytes("neu")}))))
    own = (page.config / "settings.json").read_bytes()
    page._import_backup()
    settle(page)
    assert (page.config / "settings.json").read_bytes() == own
    assert page.controller.settings.alphabet == "latin"  # and the program was not switched to the file's "de"


def test_an_alphabet_the_settings_file_names_is_switched_to_when_the_import_brings_it(
    fake_engine, injector, tk_root, tmp_path
):
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    (config / "settings.json").write_text('{"alphabet": "neu"}')  # the program fell back to latin at start
    own = (config / "settings.json").read_bytes()
    manager = DictionaryManager(user_directory=config / "alphabets")
    controller = MorseController(fake_engine, injector, manager, Settings(), use_hook=False)
    controller.start()
    dialogs = FakeDialogs(open_=str(zip_of(tmp_path, {"alphabets/neu.json": alpha_bytes("neu")})))
    page = SettingsPanel(tk_root, controller, manager, controller.apply_settings, capture_backend=NoCapture(),
                         backup_dialogs=dialogs, config_dir=config)
    page.pack()
    page.root = tk_root
    try:
        page._import_backup()
        settle(page)
        assert controller.settings.alphabet == "neu"
        assert (config / "settings.json").read_bytes() == own  # the file was not overwritten with the fallback
    finally:
        page.destroy()
        controller.stop()


def test_a_change_during_an_export_is_applied_when_it_is_over(make_panel):
    page = make_panel(FakeDialogs())
    release = threading.Event()
    page._start(lambda: release.wait(5), lambda *_: page._finish())
    page._notify.set(False)
    page.root.update()
    assert page.controller.settings.notifications is True  # held back while the job runs
    release.set()
    settle(page)
    page.root.update()
    assert page.controller.settings.notifications is False  # nothing was lost
    assert SettingsStore(page.config).load().notifications is False


def test_a_change_during_an_import_is_reported_not_lost_silently(make_panel, tmp_path):
    release, inside = threading.Event(), threading.Event()
    real = backup.import_bundle

    def slow(*args, **kwargs):
        inside.set()
        release.wait(10)
        return real(*args, **kwargs)

    dialogs = FakeDialogs(open_=str(make_source(tmp_path, '{"alphabet": "greek"}')))
    page = make_panel(dialogs)
    backup.import_bundle, original = slow, backup.import_bundle
    try:
        page._import_backup()
        deadline = time.monotonic() + 20
        while not inside.is_set() and time.monotonic() < deadline:
            page.root.update()
            time.sleep(0.005)
        page._notify.set(False)  # the user flips a switch while the import runs
        page.root.update()
        release.set()
        settle(page)
    finally:
        backup.import_bundle = original
        release.set()
    assert page.controller.settings.alphabet == "greek" and page.controller.settings.notifications is True
    assert page._notify.get() is True  # the form shows the imported state, not a value nobody has
    assert "was not applied" in page._error.cget("text")


def test_an_imported_smiley_set_is_active_at_once(fake_engine, injector, tk_root, tmp_path):
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    (config / "smileys" / "mine.json").write_bytes(
        json.dumps({"id": "mine", "codes": {"...---...": "OLD"}}).encode())
    manager = DictionaryManager(user_directory=config / "alphabets", user_smiley_directory=config / "smileys")
    controller = MorseController(fake_engine, injector, manager, Settings(smileys="mine"), use_hook=False)
    controller.start()
    new = zip_of(tmp_path, {"smileys/mine.json": json.dumps({"id": "mine", "codes": {"...---...": "NEW"}}).encode()})
    dialogs = FakeDialogs(open_=str(new))
    page = SettingsPanel(tk_root, controller, manager, controller.apply_settings, capture_backend=NoCapture(),
                         backup_dialogs=dialogs, config_dir=config)
    page.pack()
    page.root = tk_root
    try:
        assert manager.select("latin").codes["...---..."] == "OLD"
        page._import_backup()
        settle(page)
        assert manager.select("latin").codes["...---..."] == "NEW"
        assert "active now" in dialogs.calls[-1][2]
    finally:
        page.destroy()


def test_a_telecode_database_can_be_imported_while_the_telecode_alphabet_is_active(fake_engine, injector, tk_root, tmp_path):
    from mourse_decoder.telecode import build_database
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    build_database([("0001", "一")], config / "telecode.sqlite")
    build_database([("0001", "丁")], tmp_path / "new.sqlite")
    manager = DictionaryManager(user_directory=config / "alphabets", telecode_db=config / "telecode.sqlite")
    controller = MorseController(
        fake_engine, injector, manager, Settings(alphabet=DictionaryManager.TELECODE_ID), use_hook=False
    )
    controller.start()
    assert controller._decoder._store.lookup("0001") == "一"
    zip_path = zip_of(tmp_path, {"telecode.sqlite": (tmp_path / "new.sqlite").read_bytes()})
    dialogs = FakeDialogs(open_=str(zip_path))
    page = SettingsPanel(tk_root, controller, manager, controller.apply_settings, capture_backend=NoCapture(),
                         backup_dialogs=dialogs, config_dir=config)
    page.pack()
    page.root = tk_root
    try:
        page._import_backup()
        settle(page)
        assert dialogs.kinds() == ["ask", "info"], dialogs.calls  # no "Access denied" on Windows
        assert controller.settings.alphabet == DictionaryManager.TELECODE_ID  # back on the alphabet it was on
        assert controller._decoder._store.lookup("0001") == "丁"  # and reading the new file
        assert "restart" not in dialogs.calls[-1][2].lower()
    finally:
        page.destroy()
        controller.stop()


def test_a_failed_import_brings_the_telecode_alphabet_back(fake_engine, injector, tk_root, tmp_path, monkeypatch):
    from mourse_decoder.telecode import build_database
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    build_database([("0001", "一")], config / "telecode.sqlite")
    build_database([("0001", "丁")], tmp_path / "new.sqlite")
    manager = DictionaryManager(user_directory=config / "alphabets", telecode_db=config / "telecode.sqlite")
    controller = MorseController(
        fake_engine, injector, manager, Settings(alphabet=DictionaryManager.TELECODE_ID), use_hook=False
    )
    controller.start()
    zip_path = zip_of(tmp_path, {"telecode.sqlite": (tmp_path / "new.sqlite").read_bytes()})
    monkeypatch.setattr(backup, "import_bundle", lambda *a, **k: (_ for _ in ()).throw(backup.BackupError("busy")))
    dialogs = FakeDialogs(open_=str(zip_path))
    page = SettingsPanel(tk_root, controller, manager, controller.apply_settings, capture_backend=NoCapture(),
                         backup_dialogs=dialogs, config_dir=config)
    page.pack()
    page.root = tk_root
    try:
        page._import_backup()
        settle(page)
        assert dialogs.kinds() == ["ask", "error"]
        assert controller.settings.alphabet == DictionaryManager.TELECODE_ID
        assert controller._decoder._store.lookup("0001") == "一"
    finally:
        page.destroy()
        controller.stop()


def test_an_undo_through_the_restore_point_removes_what_the_import_created(make_panel, tmp_path):
    files = {"alphabets/neu.json": alpha_bytes("neu")}
    dialogs = FakeDialogs(open_=str(zip_of(tmp_path, files)))
    page = make_panel(dialogs)
    page._import_backup()
    settle(page)
    restore = next(page.config.parent.glob("MourseDecoder-backup-*.zip"))
    assert (page.config / "alphabets" / "neu.json").exists()
    dialogs.open_ = str(restore)
    page._import_backup()
    settle(page)
    assert not (page.config / "alphabets" / "neu.json").exists()
    assert "Delete: alphabets/neu.json" in dialogs.calls[-2][2] and "removed" in dialogs.calls[-1][2]
