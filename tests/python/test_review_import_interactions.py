"""Reproductions from a blind review of integration-2: backup import against pause, quit, fallback and smileys."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mourse_decoder import backup
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.i18n import set_language
from mourse_decoder.import_gate import gate
from test_backup import make_config
from test_backup_ui import FakeDialogs, NoCapture
from test_integration_interactions import run_import, shortcut_pause, start


@pytest.fixture(autouse=True)
def gate_is_free():
    gate.reset()
    yield
    gate.reset()


@pytest.fixture
def panel(tk_root, fake_engine, injector, tmp_path):
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    created = []

    def make(current_json: str, bundle_json: str | None, extra_alphabet: bool):
        config = make_config(tmp_path / "cfg" / "MourseDecoder")
        (config / "settings.json").write_text(current_json, encoding="utf-8")
        src = make_config(tmp_path / "src")
        if bundle_json is None:
            (src / "settings.json").unlink()
            (src / "function_keys.json").unlink()
        else:
            (src / "settings.json").write_text(bundle_json, encoding="utf-8")
        if extra_alphabet:
            data = {"id": "neu", "name": "Neu", "codes": {".": "e"}}
            (src / "alphabets" / "neu.json").write_text(json.dumps(data), encoding="utf-8")
        zip_path = tmp_path / "in.zip"
        backup.export_bundle(src, zip_path)
        controller, store, tray = start(fake_engine, injector, config)
        icons = []
        tray._icon = SimpleNamespace(
            icon=None, update_menu=lambda: None, title="", notify=lambda *a: None
        )
        dialogs = FakeDialogs(open_=str(zip_path))
        page = SettingsPanel(
            tk_root, controller, DictionaryManager(), tray._apply,
            capture_backend=NoCapture(), backup_dialogs=dialogs, config_dir=config, on_imported=lambda: None,
        )
        tray._window = SimpleNamespace(sync=page.refresh)
        page.pack()
        tk_root.update()
        created.append(page)
        return page, controller, store, tray, config, dialogs, icons

    yield make
    for page in created:
        page.destroy()


def test_alphabet_only_import_keeps_a_shortcut_pause(panel, fake_engine, tk_root):
    page, controller, store, tray, config, dialogs, _ = panel(
        '{"alphabet": "german", "block_input": true}', None, extra_alphabet=True
    )
    shortcut_pause(fake_engine, tray)
    assert controller.settings.enabled is False and fake_engine.active is False
    icon_after_pause = tray._icon.icon
    run_import(page, tk_root)
    print("dialogs:", dialogs.kinds())
    print("after import: settings.enabled", controller.settings.enabled, "engine.active", fake_engine.active,
          "engine.block_input", fake_engine.block_input, "tray icon unchanged:", tray._icon.icon is icon_after_pause)
    # expected: the user's pause survives an import that did not bring settings.json
    assert fake_engine.active is False, "import of alphabets only resumed Morse input (pause lost)"


def test_pause_pressed_during_the_import_is_applied_after_it(panel, fake_engine, tk_root, monkeypatch):
    page, controller, store, tray, config, dialogs, _ = panel(
        '{"alphabet": "german", "block_input": true}', '{"alphabet": "german", "block_input": true, "letter_gap_ms": 700}',
        extra_alphabet=False,
    )
    original = backup.import_bundle

    def import_with_press(*a, **k):
        fake_engine.active = False  # the user presses the on/off shortcut while the import writes
        return original(*a, **k)

    monkeypatch.setattr(backup, "import_bundle", import_with_press)
    run_import(page, tk_root)
    tray._drain_queue()  # next tray tick: the "pending" press should now be applied
    print("dialogs:", dialogs.kinds())
    print("after import+poll: settings.enabled", controller.settings.enabled, "engine.active", fake_engine.active)
    assert fake_engine.active is False, "pause pressed during the import was undone by the import's load"


def test_quit_during_the_confirmation_waits_for_the_import(panel, fake_engine, tk_root):
    page, controller, store, tray, config, dialogs, _ = panel(
        '{"alphabet": "german"}', '{"alphabet": "german", "letter_gap_ms": 777}', extra_alphabet=False
    )
    quits = []
    tray._root = SimpleNamespace(after=lambda ms, f: None, quit=lambda: quits.append("root.quit"))
    tray._icon.stop = lambda: quits.append("icon.stop")
    started = []
    original_begin = gate.begin

    def begin():
        started.append(("gate.begin after quit", bool(quits)))
        return original_begin()

    gate.begin = begin
    try:
        dialogs.on_ask = tray.quit  # the user picks Quit in the tray while the confirmation is open
        run_import(page, tk_root)
    finally:
        del gate.begin
    print("quit calls:", quits, "| pending:", tray._quit_pending, "| controller.running:", controller.running)
    print("import started:", started, "| file letter_gap:", json.loads((config / "settings.json").read_text())["letter_gap_ms"])
    assert not (quits and started), "quit went through, then the import still started (mainloop is gone: daemon writer)"


@pytest.mark.parametrize("alphabet", ["chinese-telecode", "fehlt"])
def test_import_whose_settings_cannot_be_loaded_is_not_overwritten_by_the_next_save(panel, fake_engine, tk_root, alphabet):
    page, controller, store, tray, config, dialogs, _ = panel(
        '{"alphabet": "german", "letter_gap_ms": 600}',
        json.dumps({"alphabet": alphabet, "letter_gap_ms": 777}), extra_alphabet=False,
    )
    run_import(page, tk_root)
    print("dialogs:", [(k, t[:400].replace("\n", " / ")) for k, _, t in dialogs.calls])
    on_disk = json.loads((config / "settings.json").read_text())
    print("after import: file", on_disk.get("alphabet"), on_disk.get("letter_gap_ms"),
          "| memory", controller.settings.alphabet, controller.settings.letter_gap_ms)
    tray._apply(controller.settings.replace(enabled=False))  # e.g. the tray menu "Active" afterwards
    on_disk = json.loads((config / "settings.json").read_text())
    print("after next tray change: file", on_disk.get("alphabet"), on_disk.get("letter_gap_ms"))
    assert on_disk.get("letter_gap_ms") == 777, "the imported settings.json was overwritten with the old state"


@pytest.mark.parametrize("with_settings", [False, True])
def test_a_smiley_set_named_in_settings_that_was_missing_is_switched_to_after_the_import(
    tk_root, fake_engine, injector, tmp_path, with_settings
):
    from mourse_decoder.controller import MorseController
    from mourse_decoder.settings import SettingsStore
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel
    from test_integration_interactions import real_tray

    theme.apply_theme(tk_root, "light")
    set_language("en")
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    (config / "settings.json").write_text('{"alphabet": "german", "smileys": "neu"}', encoding="utf-8")
    src = make_config(tmp_path / "src")
    if not with_settings:
        (src / "settings.json").unlink()
    else:
        (src / "settings.json").write_text('{"alphabet": "german", "smileys": "neu"}', encoding="utf-8")
    (src / "function_keys.json").unlink()
    (src / "smileys" / "neu.json").write_text(json.dumps({"id": "neu", "name": "Neu", "codes": {"........-": "Z"}}), encoding="utf-8")
    zip_path = tmp_path / "in.zip"
    backup.export_bundle(src, zip_path)
    dicts = DictionaryManager(user_directory=config / "alphabets", user_smiley_directory=config / "smileys")
    store = SettingsStore(config)
    controller = MorseController(fake_engine, injector, dicts, store.load(), use_hook=False)
    controller.start()
    tray = real_tray(controller, store)
    print("start: settings.smileys", controller.settings.smileys, "| loaded set:", repr(dicts.smiley_set_id))
    dialogs = FakeDialogs(open_=str(zip_path))
    page = SettingsPanel(tk_root, controller, dicts, tray._apply, capture_backend=NoCapture(), backup_dialogs=dialogs,
                         config_dir=config, on_imported=lambda: None)
    tray._window = SimpleNamespace(sync=page.refresh)
    page.pack()
    try:
        run_import(page, tk_root)
        print("dialogs:", dialogs.kinds())
        print("after import: settings.smileys", controller.settings.smileys, "| loaded set:", repr(dicts.smiley_set_id))
        fake_engine.letters("........-")
        print("typed for the smiley code:", repr(injector.buffer))
        assert dicts.smiley_set_id == "neu", "the imported set named in settings.json is still not active"
    finally:
        page.destroy()


def test_restoring_an_own_backup_with_unchanged_settings_keeps_the_pause(panel, fake_engine, tk_root):
    same = '{"alphabet": "german", "block_input": true}'
    page, controller, store, tray, config, dialogs, _ = panel(same, same, extra_alphabet=True)
    shortcut_pause(fake_engine, tray)
    run_import(page, tk_root)
    print("preview/ask text:", dialogs.calls[0][2][:200].replace("\n", " / "))
    print("after import: enabled", controller.settings.enabled, "engine.active", fake_engine.active,
          "engine.block_input", fake_engine.block_input)
    assert fake_engine.active is False


def test_restoring_an_alphabet_from_an_own_backup_keeps_the_pause(tk_root, fake_engine, injector, tmp_path):
    from mourse_decoder.settings import Settings, SettingsStore
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    SettingsStore(config).save(Settings(alphabet="german", block_input=True))  # written by the program itself
    zip_path = tmp_path / "own.zip"
    backup.export_bundle(config, zip_path)  # the user's own backup
    (config / "alphabets" / "mein.json").unlink()  # later the user deletes an alphabet ...
    controller, store, tray = start(fake_engine, injector, config)
    shortcut_pause(fake_engine, tray)  # ... pauses Morse input with the shortcut ...
    dialogs = FakeDialogs(open_=str(zip_path))
    page = SettingsPanel(tk_root, controller, DictionaryManager(), tray._apply, capture_backend=NoCapture(),
                         backup_dialogs=dialogs, config_dir=config, on_imported=lambda: None)
    tray._window = SimpleNamespace(sync=page.refresh)
    page.pack()
    try:
        run_import(page, tk_root)  # ... and restores the backup
        print("confirm:", dialogs.calls[0][2][:220].replace("\n", " / "))
        print("after import: enabled", controller.settings.enabled, "engine.active", fake_engine.active,
              "engine.block_input", fake_engine.block_input)
        assert fake_engine.active is False, "restoring an alphabet resumed Morse input and blocking"
    finally:
        page.destroy()
