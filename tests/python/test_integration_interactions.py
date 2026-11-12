"""Interactions between the four feature branches (calibration, backup import, pause not saved, ids).

Each branch has its own tests; these cover what only shows when they run together: the tray's ``_apply`` is the
one place that ``gate.check()`` (import running), ``_to_save`` (a shortcut pause is not saved) and the pages
(settings import live-load, calibration "Apply") all go through.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import RecordingListener
from mourse_decoder import backup, calibration
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.i18n import set_language, tr
from mourse_decoder.import_gate import ImportBusy, gate
from mourse_decoder.settings import Settings, SettingsStore
from test_backup import make_config, snapshot

try:
    from mourse_decoder.ui.tray import TrayApp
except Exception as exc:  # no display for pystray
    pytest.skip(f"UI modules not importable here: {exc!r}", allow_module_level=True)

LOCALES = Path(__file__).resolve().parents[2] / "python" / "mourse_decoder" / "locales"
PACKAGE = LOCALES.parent


@pytest.fixture(autouse=True)
def gate_is_free():
    gate.end()
    yield
    gate.end()


class CalibrationListener(RecordingListener):
    def __init__(self) -> None:
        super().__init__()
        self.timings: list[tuple[str, int]] = []
        self.ended: list[str] = []

    def on_timing(self, kind: str, ms: int) -> None:
        self.timings.append((kind, ms))

    def on_calibration_ended(self, reason: str) -> None:
        self.ended.append(reason)


def real_tray(controller, store, sync=lambda: None):
    """A TrayApp without Tk and pystray: the real ``_apply``, ``_poll_*`` and ``_drain_queue`` run on it."""
    tray = object.__new__(TrayApp)
    tray._controller = controller
    tray._store = store
    tray._dictionaries = controller._dictionaries
    tray._saved_flags = {"enabled": controller.settings.enabled, "block_input": controller.settings.block_input}
    tray._icon = SimpleNamespace(icon=None, update_menu=lambda: None, title="", notify=lambda *a: None)
    tray._window = SimpleNamespace(sync=lambda: sync())
    tray.toasts = []
    tray._toast = SimpleNamespace(show=lambda *a: tray.toasts.append(a))
    tray._set_title = lambda text: None
    tray._tooltip = lambda: ""
    tray._ui_queue = queue.Queue()
    tray.scheduled = []
    tray._root = SimpleNamespace(after=lambda ms, func: tray.scheduled.append(func))
    tray._hook_error = None
    tray._hook_error_shown = False
    return tray


def start(engine, injector, config, clock=None):
    store = SettingsStore(config)
    extra = {"clock": clock} if clock else {}
    controller = MorseController(engine, injector, DictionaryManager(), store.load(), use_hook=False, **extra)
    controller.start()
    return controller, store, real_tray(controller, store)


def shortcut_pause(engine, tray):
    engine.active = False  # what the hook does when the shortcut fires
    tray._poll_block()


def fresh_config(root):
    """The standard test config folder, with a settings.json that names a built-in alphabet."""
    config = make_config(root)
    (config / "settings.json").write_text('{"alphabet": "latin"}', encoding="utf-8")
    return config


def file_flags(config):
    s = SettingsStore(config).load()
    return s.enabled, s.block_input


# (1) import live-load vs the saved switch values ---------------------------------------------------------------


@pytest.fixture
def import_panel(tk_root, fake_engine, injector, tmp_path):
    """The settings page wired to the real ``TrayApp._apply``, like the program does it."""
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.settings_panel import SettingsPanel
    from test_backup_ui import FakeDialogs, NoCapture

    theme.apply_theme(tk_root, "light")
    set_language("en")
    created = []

    def make(current_json: str, bundle_json: str):
        config = make_config(tmp_path / "cfg" / "MourseDecoder")
        (config / "settings.json").write_text(current_json, encoding="utf-8")
        src = make_config(tmp_path / "src")
        (src / "settings.json").write_text(bundle_json, encoding="utf-8")
        zip_path = tmp_path / "in.zip"
        backup.export_bundle(src, zip_path)
        controller, store, tray = start(fake_engine, injector, config)
        dialogs = FakeDialogs(open_=str(zip_path))
        page = SettingsPanel(
            tk_root, controller, DictionaryManager(), tray._apply,
            capture_backend=NoCapture(), backup_dialogs=dialogs, config_dir=config, on_imported=lambda: None,
        )
        tray._window = SimpleNamespace(sync=page.refresh)
        page.pack()
        tk_root.update()
        created.append(page)
        return page, controller, store, tray, config, dialogs

    yield make
    for page in created:
        page.destroy()


def run_import(page, tk_root):
    import time

    page._import_backup()
    deadline = time.monotonic() + 20
    while page._backup_running and time.monotonic() < deadline:
        tk_root.update()
        time.sleep(0.005)
    tk_root.update()
    assert not page._backup_running


@pytest.mark.parametrize("bundle_enabled", [True, False])
def test_import_after_a_shortcut_pause_keeps_the_imported_switches(
    import_panel, fake_engine, tk_root, bundle_enabled
):
    """settings.json had block_input true; the shortcut paused; the bundle says block_input false (and enabled
    true or false). A later timing change must write the imported values, not the stale ones."""
    page, controller, store, tray, config, dialogs = import_panel(
        '{"alphabet": "german", "block_input": true}',
        json.dumps({"alphabet": "german", "block_input": False, "enabled": bundle_enabled}),
    )
    shortcut_pause(fake_engine, tray)
    assert controller.settings.enabled is False and file_flags(config) == (True, True)

    run_import(page, tk_root)

    assert dialogs.kinds()[-1] == "info" and "error" not in dialogs.kinds()
    assert file_flags(config) == (bundle_enabled, False)  # the import wrote them, the live-load kept them
    assert (controller.settings.enabled, controller.settings.block_input) == (False, False)
    assert (fake_engine.active, fake_engine.block_input) == (False, False)
    assert (tray._saved_flags["enabled"], tray._saved_flags["block_input"]) == (bundle_enabled, False)

    tray._apply(controller.settings.replace(letter_gap_ms=730))  # a later change of something else

    assert file_flags(config) == (bundle_enabled, False)
    assert SettingsStore(config).load().letter_gap_ms == 730
    assert (controller.settings.enabled, controller.settings.block_input) == (False, False)
    assert (fake_engine.active, fake_engine.block_input) == (False, False)
    assert fake_engine.configure_calls[-1][1] == 730


def test_import_of_the_same_switch_values_as_the_paused_state_is_not_reverted(
    import_panel, fake_engine, tk_root
):
    """The bundle says enabled false, the program is paused by the shortcut (enabled false in memory, true on disk)."""
    page, controller, store, tray, config, _ = import_panel(
        '{"alphabet": "german", "enabled": true}', '{"alphabet": "german", "enabled": false}'
    )
    shortcut_pause(fake_engine, tray)
    run_import(page, tk_root)
    assert file_flags(config)[0] is False
    tray._apply(controller.settings.replace(word_gap_ms=1700))
    assert file_flags(config)[0] is False and controller.settings.enabled is False


def test_pause_after_an_import_is_still_not_saved(import_panel, fake_engine, tk_root):
    page, controller, store, tray, config, _ = import_panel(
        '{"alphabet": "german", "block_input": true}', '{"alphabet": "german", "block_input": false}'
    )
    run_import(page, tk_root)
    shortcut_pause(fake_engine, tray)
    assert controller.settings.enabled is False
    assert file_flags(config) == (True, False)  # paused at run time only
    tray._apply(controller.settings.replace(letter_gap_ms=640))
    assert file_flags(config) == (True, False)


# (2) calibration "Apply" while an import runs --------------------------------------------------------------------


def calibration_page(tk_root, fake_engine, injector, tmp_path):
    from mourse_decoder.ui import theme
    from mourse_decoder.ui.calibration_panel import CalibrationPanel

    theme.apply_theme(tk_root, "light")
    set_language("en")
    config = fresh_config(tmp_path / "cfg" / "MourseDecoder")
    controller, store, tray = start(fake_engine, injector, config)
    page = CalibrationPanel(tk_root, controller, tray._apply)
    page.pack()
    tk_root.update()
    page._toggle()
    for kind, values in (
        ("press_ms", [110, 120, 130, 115, 125, 118]),
        ("press_ms", [380, 400, 420, 390, 410, 405]),
        ("gap_ms", [1, 800, 850, 900, 820, 880]),
        ("gap_ms", [1, 2000, 2200, 2100, 2300]),
    ):
        for ms in values:
            page.add_timing(kind, ms)
    return page, controller, store, tray, config


def test_calibration_apply_during_an_import_shows_the_busy_text(tk_root, fake_engine, injector, tmp_path):
    page, controller, store, tray, config = calibration_page(tk_root, fake_engine, injector, tmp_path)
    before, files = controller.settings, snapshot(config)
    assert gate.begin()
    page._apply()
    assert page._notes.get() == tr("backup.err.busy") == "An import is running. Try again when it is finished."
    assert page._notes.get() != tr("calibration.apply.failed")
    assert controller.settings == before and snapshot(config) == files  # nothing applied, nothing written
    gate.end()
    page._apply()  # the same results apply once the import is over
    assert page._notes.get() == tr("calibration.applied")
    assert controller.settings.letter_gap_ms != before.letter_gap_ms
    assert SettingsStore(config).load() == controller.settings


def test_calibration_apply_failure_that_is_not_the_gate_keeps_the_generic_text(
    tk_root, fake_engine, injector, tmp_path
):
    page, controller, store, tray, config = calibration_page(tk_root, fake_engine, injector, tmp_path)

    def refuse(settings):
        raise ValueError("Ungültig")

    page._apply_settings = refuse
    page._apply()
    assert page._notes.get() == tr("calibration.apply.failed")


def test_busy_text_exists_in_every_language_and_is_what_the_gate_raises(tmp_path):
    for path in sorted(LOCALES.glob("*.json")):
        set_language(path.stem)
        try:
            with pytest.raises(ImportBusy) as info:
                assert gate.begin()
                gate.check()
            gate.end()
            assert str(info.value) == tr("backup.err.busy") != "backup.err.busy", path.name
        finally:
            gate.end()
    set_language("en")


# (3)/(4) calibration vs shortcut pause, enabled=False, the gate, the poll ---------------------------------------


def test_measuring_goes_on_through_a_shortcut_pause_and_resume(fake_engine, injector, tmp_path):
    config = fresh_config(tmp_path / "cfg")
    controller, store, tray = start(fake_engine, injector, config)
    listener = CalibrationListener()
    controller.set_listener(listener)
    assert controller.set_calibrating(True)
    shortcut_pause(fake_engine, tray)
    assert controller.settings.enabled is False and controller.calibrating  # the poll changes only ``enabled``
    assert fake_engine.measuring is True and controller._muted is True
    fake_engine.emit("press_ms", "120")
    fake_engine.emit("gap_ms", "300")
    assert listener.timings == [("press_ms", 120), ("gap_ms", 300)]
    fake_engine.active = True  # resume by the shortcut while measuring
    tray._poll_block()
    assert controller.settings.enabled is True and controller.calibrating and controller._muted is True
    fake_engine.letters("...")
    assert injector.calls == []  # still muted: the page, not the switch, decides
    controller.set_calibrating(False)
    assert not controller.calibrating
    fake_engine.letters("...")
    assert injector.buffer == "S"
    assert file_flags(config) == (True, False)  # nothing of this was saved as a change


def test_measuring_with_enabled_false_reports_timings_and_stays_silent_after_it(fake_engine, injector, tmp_path):
    config = fresh_config(tmp_path / "cfg")
    (config / "settings.json").write_text('{"alphabet": "german", "enabled": false}', encoding="utf-8")
    controller, store, tray = start(fake_engine, injector, config)
    listener = CalibrationListener()
    controller.set_listener(listener)
    assert controller.settings.enabled is False
    assert controller.set_calibrating(True)
    fake_engine.emit("press_ms", "100")
    assert listener.timings == [("press_ms", 100)]
    controller.set_calibrating(False)
    fake_engine.letters("...")
    assert injector.calls == []  # switched off stays switched off
    assert controller.settings.enabled is False


def test_calibration_polls_during_an_import_write_no_files_and_the_gate_skips_only_the_switch_polls(
    fake_engine, injector, tmp_path
):
    config = fresh_config(tmp_path / "cfg")
    now = [1000.0]
    controller, store, tray = start(fake_engine, injector, config, clock=lambda: now[0])
    listener = CalibrationListener()
    controller.set_listener(listener)
    controller.set_calibrating(True)
    files = snapshot(config)
    assert gate.begin()
    fake_engine.active = False  # a shortcut press during the import
    fake_engine.emit("press_ms", "120")
    now[0] += controller.CALIBRATION_MAX_S + 1
    tray._drain_queue()
    assert listener.ended == ["timeout"] and not controller.calibrating  # the calibration poll still runs
    assert controller.settings.enabled is True  # the switch poll waits: the press stays pending
    assert snapshot(config) == files
    assert len(tray.scheduled) == 1  # the loop is scheduled again
    gate.end()
    tray._drain_queue()
    assert controller.settings.enabled is False  # now the pending press is applied
    assert file_flags(config) == (True, False)


def test_tray_apply_is_refused_while_an_import_runs_and_allowed_for_its_own_load(fake_engine, injector, tmp_path):
    config = fresh_config(tmp_path / "cfg")
    controller, store, tray = start(fake_engine, injector, config)
    assert gate.begin()
    before = snapshot(config)
    with pytest.raises(ImportBusy):
        tray._apply(controller.settings.replace(letter_gap_ms=900))
    assert snapshot(config) == before and controller.settings.letter_gap_ms != 900
    with gate.allow():
        tray._apply(controller.settings.replace(letter_gap_ms=900))
    assert SettingsStore(config).load().letter_gap_ms == 900


def test_gate_controller_and_tray_never_wait_for_each_other(fake_engine, injector, tmp_path):
    """The tray takes the gate guard, then the controller lock; the controller never takes the gate. Run the
    paths against each other from three threads: a lock-order cycle would hang here."""
    config = fresh_config(tmp_path / "cfg")
    controller, store, tray = start(fake_engine, injector, config)
    controller.set_listener(CalibrationListener())
    failures: list[BaseException] = []

    def guarded(work):
        def run():
            try:
                work()
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)

        return run

    def importer():
        for i in range(60):
            if gate.begin():
                try:
                    with gate.allow():
                        tray._apply(controller.settings.replace(letter_gap_ms=500 + i % 7))
                finally:
                    gate.end()

    def calibrator():
        for _ in range(150):
            controller.set_calibrating(True)
            fake_engine.emit("press_ms", "100")
            controller.check_calibration_timeout()
            controller._end_calibrating("stopped")

    def poller():
        for _ in range(150):
            tray._drain_queue()
            gate.blocked()

    threads = [threading.Thread(target=guarded(w), daemon=True) for w in (importer, calibrator, poller)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), "deadlock between gate, controller lock and tray"
    # ImportBusy from the poller's switch/apply is fine; anything else is not
    assert all(isinstance(f, ImportBusy) for f in failures), failures


# (5) ids: old files, hostile ids, calibration and backup ----------------------------------------------------------

OLD_FILE = '{"dot_threshold_ms": 180, "letter_gap_ms": 500, "enabled": true, "removed_option": 1}'
HOSTILE_FILE = '{"alphabet": "//host/share/x", "smileys": "..\\\\evil", "dot_threshold_ms": 250}'


def test_a_settings_file_of_an_older_version_starts_and_calibrates(fake_engine, injector, tmp_path):
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "settings.json").write_text(OLD_FILE, encoding="utf-8")
    controller, store, tray = start(fake_engine, injector, config)
    assert (controller.settings.dot_threshold_ms, controller.settings.letter_gap_ms) == (180, 500)
    assert controller.settings.alphabet == Settings().alphabet
    fake_engine.letters("...")
    assert injector.buffer == "S"
    proposal = calibration.propose([110, 120, 130], [380, 400, 420], [800, 850, 900], [2000, 2200, 2100])
    tray._apply(calibration.apply_proposal(controller.settings, proposal))
    assert SettingsStore(config).load() == controller.settings
    assert not list(config.glob("settings.json.bak*"))


def test_a_hostile_id_in_settings_json_is_reset_and_the_app_starts(fake_engine, injector, tmp_path):
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "settings.json").write_text(HOSTILE_FILE, encoding="utf-8")
    controller, store, tray = start(fake_engine, injector, config)
    s = controller.settings
    assert (s.alphabet, s.smileys, s.dot_threshold_ms) == (Settings().alphabet, Settings().smileys, 250)
    fake_engine.letters("...")
    assert injector.buffer == "S"
    controller.set_calibrating(True)  # the calibration paths work on the repaired settings
    controller.set_calibrating(False)
    tray._apply(s.replace(letter_gap_ms=650))
    saved = json.loads((config / "settings.json").read_text(encoding="utf-8"))
    assert "//" not in saved["alphabet"] and saved["letter_gap_ms"] == 650


@pytest.mark.parametrize("field", ["alphabet", "smileys"])
def test_settings_replace_with_a_hostile_id_is_refused_by_validate_apply_and_save(
    fake_engine, injector, tmp_path, field
):
    config = fresh_config(tmp_path / "cfg")
    controller, store, tray = start(fake_engine, injector, config)
    bad = controller.settings.replace(**{field: "//host/share/x"})
    assert bad.validate()
    files = snapshot(config)
    with pytest.raises(ValueError):
        tray._apply(bad)
    assert snapshot(config) == files and controller.settings != bad
    assert (tray._saved_flags["enabled"], tray._saved_flags["block_input"]) == (True, False)


def test_a_bundle_with_a_hostile_id_is_refused_before_anything_is_written(tmp_path):
    config = make_config(tmp_path / "cfg" / "MourseDecoder")
    src = make_config(tmp_path / "src")
    (src / "settings.json").write_text(HOSTILE_FILE, encoding="utf-8")
    zip_path = tmp_path / "in.zip"
    backup.export_bundle(src, zip_path)
    files = snapshot(config)
    with pytest.raises(backup.BackupError) as info:
        backup.inspect_bundle(zip_path, config)
    assert info.value.code == "settings_bad_id"
    with pytest.raises(backup.BackupError) as info:
        backup.import_bundle(zip_path, config)
    assert info.value.code == "settings_bad_id"
    assert snapshot(config) == files and not list(config.parent.glob("*backup*"))


# (6) locale files ------------------------------------------------------------------------------------------------


def _read_locale(path: Path):
    raw = path.read_bytes()
    seen: list[str] = []

    def hook(pairs):
        keys = [k for k, _ in pairs]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            seen.extend(sorted(dupes))
        return dict(pairs)

    return raw, json.loads(raw.decode("utf-8"), object_pairs_hook=hook), seen


LOCALE_FILES = sorted(LOCALES.glob("*.json"))


def test_there_are_seven_locale_files():
    assert [p.stem for p in LOCALE_FILES] == ["de-saar", "de", "en", "es", "fr", "it", "pt"]


@pytest.mark.parametrize("path", LOCALE_FILES, ids=lambda p: p.stem)
def test_locale_has_no_duplicate_keys_and_clean_line_endings(path):
    raw, data, dupes = _read_locale(path)
    assert dupes == [], f"{path.name}: duplicate keys {dupes}"
    assert b"\r" not in raw.replace(b"\r\n", b"") and (raw.count(b"\r\n") in (0, raw.count(b"\n"))), "mixed line endings"
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\n") == _read_locale(LOCALES / "en.json")[0].endswith(b"\n")


def test_all_locales_have_the_same_keys_and_placeholders():
    _, english, _ = _read_locale(LOCALES / "en.json")
    strings = english["strings"]
    for path in LOCALE_FILES:
        _, data, _ = _read_locale(path)
        other = data["strings"]
        assert set(other) == set(strings), (path.name, sorted(set(strings) ^ set(other))[:10])
        for key, text in strings.items():
            assert sorted(re.findall(r"\{(\w*)\}", other[key])) == sorted(re.findall(r"\{(\w*)\}", text)), (path.name, key)


def test_the_keys_of_the_merged_features_exist_in_every_locale():
    _, english, _ = _read_locale(LOCALES / "en.json")
    wanted = {k for k in english["strings"] if k.startswith(("backup.err.", "calibration.")) or k == "nav.calibration"}
    assert "nav.calibration" in wanted and "backup.err.busy" in wanted
    assert len([k for k in wanted if k.startswith("backup.err.")]) >= 10
    for path in LOCALE_FILES:
        strings = _read_locale(path)[1]["strings"]
        assert not [k for k in wanted if not strings.get(k)], path.name


def test_every_literal_tr_key_and_backup_error_code_of_the_code_exists():
    _, english, _ = _read_locale(LOCALES / "en.json")
    strings = english["strings"]
    literal = re.compile(r"""\btr\(\s*f?["']([a-z0-9_.]+)["']""")
    missing = []
    for source in [*PACKAGE.glob("*.py"), *(PACKAGE / "ui").glob("*.py")]:
        for key in literal.findall(source.read_text(encoding="utf-8")):
            if key not in strings:
                missing.append(f"{source.name}: {key}")
    codes = set(re.findall(r"""BackupError\(\s*["']([a-z_0-9]+)["']""", (PACKAGE / "backup.py").read_text("utf-8")))
    missing += [f"backup.err.{code}" for code in sorted(codes) if f"backup.err.{code}" not in strings]
    for phase in calibration.PHASES:
        missing += [k for k in (f"calibration.step.{phase}", f"calibration.hint.{phase}") if k not in strings]
    assert missing == []


# ---- findings of the blind reviews (reproductions of the reviewer are in test_review_import_interactions.py) ----


def test_a_pending_shortcut_press_survives_any_apply_that_does_not_set_the_switch(fake_engine, injector, tmp_path):
    config = fresh_config(tmp_path / "cfg")
    controller, store, tray = start(fake_engine, injector, config)
    fake_engine.active = False  # the hook flipped it, the poll has not run yet
    controller.apply_settings(controller.settings.replace(letter_gap_ms=700))
    assert fake_engine.active is False
    toggled = controller.poll_active_toggle()
    assert toggled is not None and toggled.enabled is False  # the poll still sees the press


def test_apply_that_sets_the_switch_itself_wins_over_the_engine_state(fake_engine, injector, tmp_path):
    config = fresh_config(tmp_path / "cfg")
    controller, store, tray = start(fake_engine, injector, config)
    controller.apply_settings(controller.settings.replace(enabled=False))  # tray menu
    assert fake_engine.active is False
    controller.apply_settings(controller.settings.replace(enabled=True))
    assert fake_engine.active is True
    fake_engine.active = False
    controller.apply_settings(controller.settings.replace(enabled=True, letter_gap_ms=650))  # keeps the press
    assert fake_engine.active is False


def test_fallback_of_an_import_that_cannot_be_loaded_keeps_the_imported_file(import_panel, fake_engine, tk_root):
    page, controller, store, tray, config, dialogs = import_panel(
        '{"alphabet": "german", "letter_gap_ms": 600}',
        json.dumps({"alphabet": "chinese-telecode", "letter_gap_ms": 777, "smileys": "gibts-nicht"}),
    )
    run_import(page, tk_root)
    s = controller.settings
    assert (s.alphabet, s.smileys, s.letter_gap_ms) == ("latin", "", 777)  # fallback in memory, rest imported
    assert dialogs.kinds()[-1] == "info" and "could not load" not in dialogs.calls[-1][2]

    def pause():
        shortcut_pause(fake_engine, tray)

    def timing():
        tray._apply(controller.settings.replace(word_gap_ms=1800))

    for change in (pause, timing):
        change()
        on_disk = json.loads((config / "settings.json").read_text(encoding="utf-8"))
        assert (on_disk["alphabet"], on_disk["smileys"], on_disk["letter_gap_ms"]) == (
            "chinese-telecode", "gibts-nicht", 777,
        )
    # the user choosing another alphabet is saved
    tray._apply(controller.settings.replace(alphabet="greek"))
    assert json.loads((config / "settings.json").read_text(encoding="utf-8"))["alphabet"] == "greek"


def test_a_smiley_set_that_is_still_missing_does_not_break_later_changes(fake_engine, injector, tmp_path):
    config = fresh_config(tmp_path / "cfg")
    (config / "settings.json").write_text('{"alphabet": "latin", "smileys": "neu"}', encoding="utf-8")
    dicts = DictionaryManager(user_directory=config / "alphabets", user_smiley_directory=config / "smileys")
    store = SettingsStore(config)
    controller = MorseController(fake_engine, injector, dicts, store.load(), use_hook=False)
    controller.start()
    controller.apply_settings(controller.settings.replace(letter_gap_ms=640))  # no error, set still missing
    assert dicts.smiley_set_id == "" and controller.settings.smileys == "neu"
    (config / "smileys" / "neu.json").write_text(
        json.dumps({"id": "neu", "name": "Neu", "codes": {"........-": "Z"}}), encoding="utf-8"
    )
    controller.apply_settings(controller.settings.replace(letter_gap_ms=650))  # now it loads
    assert dicts.smiley_set_id == "neu"
    fake_engine.letters("........-")
    assert injector.buffer == "Z"


def test_scratch_folders_are_removed_at_exit_even_if_their_thread_never_finishes():
    manager = backup._scratch()
    path = manager.__enter__()
    try:
        assert path.is_dir() and path in backup._live_scratch
        backup._clean_scratch()
        assert not path.exists() and path not in backup._live_scratch
    finally:
        manager.__exit__(None, None, None)  # still fine after the hook ran


def test_quit_waits_from_the_first_job_of_the_page_until_the_import_is_done(import_panel, fake_engine, tk_root):
    import time

    page, controller, store, tray, config, dialogs = import_panel(
        '{"alphabet": "german"}', '{"alphabet": "german", "letter_gap_ms": 777}'
    )
    quits: list[str] = []
    tray._root = SimpleNamespace(after=lambda ms, f: None, quit=lambda: quits.append("root.quit"))
    tray._icon.stop = lambda: quits.append("icon.stop")
    page._import_backup()  # the check job is running now
    assert gate.busy()
    tray.quit()
    assert quits == [] and tray._quit_pending
    deadline = time.monotonic() + 20
    while page._backup_running and time.monotonic() < deadline:
        tk_root.update()
        time.sleep(0.005)
    assert not gate.busy()
    assert json.loads((config / "settings.json").read_text(encoding="utf-8"))["letter_gap_ms"] == 777  # import was done
    tray._drain_queue()
    assert quits == ["icon.stop", "root.quit"]


def test_the_tray_menu_icon_and_tooltip_follow_an_import(fake_engine, injector, tmp_path):
    config = fresh_config(tmp_path / "cfg")
    controller, store, tray = start(fake_engine, injector, config)
    built, titles = [], []
    tray._build_menu = lambda: built.append("menu") or "menu"
    tray._set_title = titles.append
    tray._tooltip = lambda: "tip"
    tray._rebuild_menu()
    assert built == ["menu"] and tray._icon.menu == "menu" and titles == ["tip"] and tray._icon.icon is not None


def test_the_main_window_rebuilds_the_tray_menu_after_an_import(tk_root, fake_engine, injector, tmp_path):
    import tkinter as tk

    from mourse_decoder.ui import theme
    from mourse_decoder.ui.main_window import MainWindow

    theme.apply_theme(tk_root, "light")
    set_language("en")
    config = fresh_config(tmp_path / "cfg" / "MourseDecoder")
    controller, store, tray = start(fake_engine, injector, config)
    rebuilt = []
    top = tk.Toplevel(tk_root)
    try:
        window = MainWindow(
            top, controller, DictionaryManager(), apply_settings=tray._apply,
            on_alphabets_changed=lambda: rebuilt.append(1), on_hide=lambda: None, on_quit=lambda: None,
            config_dir=config,
        )
        page = window._page_widget("settings")
        before = len(rebuilt)
        page._on_imported()
        assert len(rebuilt) == before + 1  # the settings page reports an import: the tray menu is rebuilt
    finally:
        top.destroy()


def test_gc_stays_off_while_a_backup_job_runs_and_tk_variables_are_freed_on_the_tk_thread(
    import_panel, fake_engine, tk_root
):
    import gc
    import time
    import tkinter as tk

    page, *_ = import_panel('{"alphabet": "german"}', '{"alphabet": "german", "letter_gap_ms": 777}')
    freed_on: list[str] = []

    class Var(tk.StringVar):
        def __del__(self):
            freed_on.append(threading.current_thread().name)
            super().__del__()

    class Holder:  # a cycle that only the collector can free, like a destroyed page
        def __init__(self):
            self.me = self
            self.var = Var(tk_root)

    Holder()
    seen = {}

    def work():
        seen["gc_enabled"] = gc.isenabled()
        junk = [[object()] for _ in range(300_000)]  # allocations that would start a collection
        return len(junk)

    results = []
    started = time.monotonic()
    page._start(work, lambda result, error: (results.append(result), page._finish()))
    deadline = time.monotonic() + 30
    while page._backup_running and time.monotonic() < deadline:
        tk_root.update()
        time.sleep(0.005)
    assert results == [300_000] and seen["gc_enabled"] is False
    assert gc.isenabled()
    assert "backup" not in freed_on, "a Tk variable was freed on the worker thread"
    assert time.monotonic() - started < 5


def test_gc_is_back_on_when_the_page_is_closed_during_a_job(import_panel, fake_engine, tk_root):
    import gc
    import time

    page, *_ = import_panel('{"alphabet": "german"}', '{"alphabet": "german"}')
    release = threading.Event()
    page._start(lambda: release.wait(10), lambda result, error: page._finish())
    assert not gc.isenabled()
    page.destroy()
    assert gc.isenabled() and not gate.busy()
    release.set()
    time.sleep(0.05)


@pytest.mark.parametrize("bad", ["Upper", "Latin", "dot.ted", "ünï", "//host/x", "-x"])
@pytest.mark.parametrize("field", ["alphabet", "smileys"])
def test_an_invalid_id_in_settings_json_is_reported_not_silently_reset(tmp_path, field, bad):
    from mourse_decoder import app

    set_language("en")
    config = tmp_path / "cfg"
    config.mkdir()
    text = json.dumps({field: bad, "dot_threshold_ms": 250}, ensure_ascii=False).encode("utf-8")
    (config / "settings.json").write_bytes(text)
    store = SettingsStore(config)
    settings = store.load()
    assert getattr(settings, field) == getattr(Settings(), field) and settings.dot_threshold_ms == 250
    messages = app.settings_warnings(store)
    assert len(messages) == 1 and bad[:40] in messages[0] and "settings.json" in messages[0]
    assert (config / "settings.json").read_bytes() == text  # the file is still as the user left it
    set_language("de")
    try:
        assert app.settings_warnings(store)[0] != messages[0]
    finally:
        set_language("en")


def test_valid_ids_and_old_files_give_no_warning_and_stay_byte_identical(tmp_path):
    config = tmp_path / "cfg"
    config.mkdir()
    for text in (OLD_FILE, '{"alphabet": "mein_alphabet-2", "smileys": "my-set_1"}', '{"smileys": ""}'):
        (config / "settings.json").write_text(text, encoding="utf-8")
        store = SettingsStore(config)
        store.load()
        assert store.warnings == [] and (config / "settings.json").read_text(encoding="utf-8") == text
