"""The external trigger in the program: service start/stop in both modes, toasts, follow-up on settings and imports.

No Tk window and no real pipe: the tray is built without ``__init__``, the service is a fake or has a fake server.
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest
from mourse_decoder import app
from mourse_decoder.controller import MorseController
from mourse_decoder.dictionary import DictionaryManager
from mourse_decoder.i18n import set_language, tr
from mourse_decoder.import_gate import gate
from mourse_decoder.settings import Settings, SettingsStore
from mourse_decoder.trigger import TriggerEvent, TriggerLog, TriggerService

try:
    import mourse_decoder.ui.tray as tray_mod
except Exception as exc:  # no display for pystray
    pytest.skip(f"UI modules not importable here: {exc!r}", allow_module_level=True)

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"


@pytest.fixture(autouse=True)
def clean_state():
    gate.reset()
    set_language("en")
    yield
    gate.reset()
    set_language("en")


class FakeServer:
    def __init__(self, name, sddl, handler):
        self.stopped = False

    def start(self):
        return True

    def stop(self):
        self.stopped = True


class FakeService:
    """Records ``apply`` with what the controller ran at that moment."""

    def __init__(self, controller=None, fail=False):
        self.controller, self.fail, self.applied, self.status = controller, fail, [], "off"

    def apply(self, settings):
        self.applied.append((settings, self.controller.settings.trigger_enabled if self.controller else None))
        if self.fail:
            raise RuntimeError("boom")


def make_tray(controller, store, service=None):
    tray = object.__new__(tray_mod.TrayApp)
    tray._controller = controller
    tray._store = store
    tray._saved_flags = {"enabled": controller.settings.enabled, "block_input": controller.settings.block_input}
    tray._icon = SimpleNamespace(icon=None, update_menu=lambda: None, title="")
    tray._window = SimpleNamespace(sync=lambda: None)
    tray.toasts = []  # ((text, on), id of the thread that showed it)
    tray._toast = SimpleNamespace(show=lambda *a: tray.toasts.append((a, threading.get_ident())))
    tray._set_title = lambda text: None
    tray._tooltip = lambda: ""
    tray._ui_queue = queue.Queue()
    tray._trigger = service
    tray._trigger_guard = threading.Lock()
    return tray


def drain(tray):
    """What the Tk loop does with the queue, on the calling thread."""
    while not tray._ui_queue.empty():
        tray._ui_queue.get_nowait()()


def started(engine, injector, tmp_path, **changes):
    store = SettingsStore(tmp_path)
    store.save(Settings(**changes))
    controller = MorseController(engine, injector, DictionaryManager(), store.load(), use_hook=False)
    controller.start()
    return controller, store


def rejections(tray):
    return [shown for shown, _ in tray.toasts if shown[0] == tr("trigger.toast.rejected")]


# --- toasts ------------------------------------------------------------------------------------------------------


def accepted(slot=5, at=0.0):
    return TriggerEvent(at, slot, "ok")


def refused(code="not_allowed"):
    return TriggerEvent(0.0, 5, code)


def test_toasting_log_reports_every_request_and_still_logs_it():
    seen = []
    log = tray_mod.ToastingLog()
    log.on_event = seen.append
    first, second = log.add(5, "ok"), log.add(None, "bad_request")
    assert seen == [first, second]
    assert log.events() == [first, second]


def test_toasting_log_survives_a_failing_notice_and_works_without_one():
    log = tray_mod.ToastingLog()
    log.add(1, "ok")  # no on_event
    log.on_event = lambda event: 1 / 0
    event = log.add(2, "ok")
    assert log.events()[-1] == event


def test_accepted_trigger_toast_is_shown_on_the_ui_thread_even_with_notices_off(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path, notifications=False)
    tray = make_tray(controller, store)
    at = time.mktime((2026, 1, 2, 13, 4, 5, 0, 0, -1))
    worker = threading.Thread(target=tray._on_trigger_event, args=(accepted(57, at),))
    worker.start()
    worker.join()
    assert tray.toasts == []  # nothing was shown on the server's thread
    drain(tray)
    ((shown, thread),) = tray.toasts
    assert shown == ("Macro F57 started by an external program at 13:04:05",)
    assert thread == threading.get_ident()


def test_every_accepted_trigger_gets_its_toast(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path)
    tray = make_tray(controller, store)
    for slot in (1, 2, 3):
        tray._on_trigger_event(accepted(slot))
    drain(tray)
    assert len(tray.toasts) == 3


def test_refused_requests_toast_once_per_minute(fake_engine, injector, tmp_path, monkeypatch):
    now = [5.0]  # a monotonic clock may be below 60 s right after the start
    monkeypatch.setattr(
        tray_mod, "time", SimpleNamespace(monotonic=lambda: now[0], strftime=time.strftime, localtime=time.localtime)
    )
    controller, store = started(fake_engine, injector, tmp_path)
    tray = make_tray(controller, store)
    for at, code in [(5.0, "not_allowed"), (6.0, "busy"), (30.0, "rate_limited"), (64.9, "inactive")]:
        now[0] = at
        tray._on_trigger_event(refused(code))
        tray._on_trigger_event(accepted())  # an accepted one neither is held back nor resets the limit
    drain(tray)
    assert len(rejections(tray)) == 1 and rejections(tray)[0][1:] == (False,)
    now[0] = 65.0
    tray._on_trigger_event(refused())
    now[0] = 66.0
    tray._on_trigger_event(refused())
    drain(tray)
    assert len(rejections(tray)) == 2


def test_the_log_feeds_the_toasts_through_the_service(fake_engine, injector, tmp_path):
    """The real chain: service, handle_request, ToastingLog.add, tray; for a refused and an accepted request."""
    controller, store = started(fake_engine, injector, tmp_path, trigger_enabled=True)
    tray = make_tray(controller, store)
    log = tray_mod.ToastingLog()
    log.on_event = tray._on_trigger_event
    controller.trigger_slot = lambda number: "ok" if number == 7 else "not_allowed"
    handlers = []
    service = TriggerService(
        controller, log, blocked=lambda: False,
        server_factory=lambda name, sddl, handler: handlers.append(handler) or FakeServer(name, sddl, handler),
        sid=lambda: SID,
    )
    service.start(controller.settings)
    assert handlers[0](b"MOURSE1 TRIGGER F9\n") == b"ERR not_allowed\n"
    assert handlers[0](b"MOURSE1 TRIGGER F7\n") == b"OK\n"
    drain(tray)
    texts = [shown[0] for shown, _ in tray.toasts]
    assert texts[0] == tr("trigger.toast.rejected") and texts[1].startswith("Macro F7 started")


# --- the service follows the settings ----------------------------------------------------------------------------


def test_apply_tells_the_service_after_the_controller_and_saves_the_switch(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path)
    service = FakeService(controller)
    tray = make_tray(controller, store, service)
    tray._apply(controller.settings.replace(trigger_enabled=True))
    assert [(s.trigger_enabled, seen) for s, seen in service.applied] == [(True, True)]  # controller was first
    assert store.load().trigger_enabled is True  # persisted without being one of SAVED_FIELDS
    tray._apply(controller.settings.replace(trigger_enabled=False))
    assert service.applied[-1][0].trigger_enabled is False and store.load().trigger_enabled is False


def test_a_pause_by_the_shortcut_keeps_the_trigger_switch(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path, trigger_enabled=True)
    service = FakeService(controller)
    tray = make_tray(controller, store, service)
    fake_engine.active = False
    tray._poll_block()
    assert service.applied[-1][0].trigger_enabled is True and service.applied[-1][0].enabled is False


def test_a_failing_service_does_not_stop_the_save(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path)
    tray = make_tray(controller, store, FakeService(controller, fail=True))
    tray._apply(controller.settings.replace(letter_gap_ms=700))
    assert store.load().letter_gap_ms == 700


def test_the_service_follows_only_after_the_settings_are_saved(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path)
    seen = []

    class Watching(FakeService):
        def apply(self, settings):
            seen.append(store.load().trigger_enabled)  # what is on disk when the pipe changes
            super().apply(settings)

    tray = make_tray(controller, store, Watching(controller))
    tray._apply(controller.settings.replace(trigger_enabled=True))
    assert seen == [True]


def test_a_failing_save_leaves_the_pipe_as_it_was(fake_engine, injector, tmp_path, monkeypatch):
    controller, store = started(fake_engine, injector, tmp_path)
    service = FakeService(controller)
    tray = make_tray(controller, store, service)

    def broken(settings):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", broken)
    with pytest.raises(OSError):
        tray._apply(controller.settings.replace(trigger_enabled=True))
    assert service.applied == []  # the file says "off", so the pipe stays closed


def test_a_tray_without_a_service_applies_as_before(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path)
    tray = make_tray(controller, store, None)
    tray._apply(controller.settings.replace(letter_gap_ms=700))
    assert store.load().letter_gap_ms == 700


def test_an_import_that_switches_the_trigger_off_closes_the_pipe(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path, trigger_enabled=True)
    servers = []
    service = TriggerService(
        controller, TriggerLog(), blocked=lambda: False,
        server_factory=lambda *args: servers.append(FakeServer(*args)) or servers[-1], sid=lambda: SID,
    )
    service.start(controller.settings)
    assert service.status == "running"
    tray = make_tray(controller, store, service)
    assert gate.begin()
    with gate.allow():  # the import loads the settings.json it wrote (trigger_enabled forced off by backup.py)
        tray._apply(controller.settings.replace(trigger_enabled=False))
    gate.end()
    assert service.status == "off" and servers[0].stopped is True
    assert store.load().trigger_enabled is False


def test_import_with_a_fallback_alphabet_hands_the_service_what_runs(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path, trigger_enabled=True)
    service = FakeService(controller)
    tray = make_tray(controller, store, service)
    assert gate.begin()
    with gate.allow():
        tray._apply(controller.settings.replace(alphabet="gone", trigger_enabled=False))
    gate.end()
    live = service.applied[-1][0]
    assert live.alphabet == "latin" and live is controller.settings and live.trigger_enabled is False


# --- quit ----------------------------------------------------------------------------------------------------------


def quitting_tray(controller, store, service, events):
    tray = make_tray(controller, store, service)
    tray._icon = SimpleNamespace(stop=lambda: events.append("icon.stop"))
    tray._root = SimpleNamespace(quit=lambda: events.append("root.quit"))
    tray._quit_pending = False
    return tray


def test_quit_closes_the_pipe_before_the_controller_stops(fake_engine, injector, tmp_path, monkeypatch):
    controller, store = started(fake_engine, injector, tmp_path)
    events = []
    monkeypatch.setattr(controller, "stop", lambda: events.append("controller.stop"))
    service = SimpleNamespace(stop=lambda: events.append("service.stop"))
    tray_mod.TrayApp.quit(quitting_tray(controller, store, service, events))
    assert events == ["service.stop", "controller.stop", "icon.stop", "root.quit"]


def test_a_request_after_quit_began_is_answered_inactive_not_ok(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path)
    keys = controller.function_keys
    controller._function_keys = keys.replace_slot(keys.slot(13).replace(macro="hi", triggerable=True))
    tray_mod.TrayApp.quit(quitting_tray(controller, store, None, []))
    assert controller.trigger_slot(13) == "inactive"


def test_quit_goes_on_when_the_service_cannot_stop(fake_engine, injector, tmp_path):
    controller, store = started(fake_engine, injector, tmp_path)
    events = []

    def boom():
        raise RuntimeError("boom")

    tray_mod.TrayApp.quit(quitting_tray(controller, store, SimpleNamespace(stop=boom), events))
    assert events == ["icon.stop", "root.quit"] and not controller.running


# --- app.py: both modes ------------------------------------------------------------------------------------------


class RecordingService:
    instances: list[RecordingService] = []
    events: list[str] = []

    def __init__(self, controller, log, blocked=None, announce=None, **kwargs):
        self.controller, self.log, self.announce, self.started_with = controller, log, announce, None
        RecordingService.instances.append(self)

    def start(self, settings):
        self.started_with = settings
        RecordingService.events.append("trigger.start")

    def stop(self):
        RecordingService.events.append("trigger.stop")


@pytest.fixture
def program(monkeypatch, tmp_path, fake_engine):
    """app.main with a fake native module, a temp config folder and a fake service; nothing real is touched."""
    RecordingService.instances, RecordingService.events = [], []
    SettingsStore(tmp_path).save(Settings(trigger_enabled=True))
    native = SimpleNamespace(MorseEngine=lambda trigger: fake_engine, hook_error=lambda: None)
    monkeypatch.setattr(app, "_exit_when_launcher_dies", lambda: None)
    monkeypatch.setattr(app, "load_native", lambda: native)
    monkeypatch.setattr(app, "SettingsStore", lambda: SettingsStore(tmp_path))
    monkeypatch.setattr(app, "clean_orphaned_scratch", lambda: None)
    monkeypatch.setattr(app.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(app, "TriggerService", RecordingService)
    monkeypatch.setattr(fake_engine, "stop", lambda: RecordingService.events.append("controller.stop"))
    return SimpleNamespace(native=native, directory=tmp_path)


def test_headless_starts_the_service_after_the_function_keys_and_stops_it_first(program, monkeypatch):
    monkeypatch.setattr(
        app, "_start_function_keys", lambda *a: RecordingService.events.append("function_keys") or []
    )
    program.native.hook_error = lambda: "hook gone"  # ends the loop after one round
    assert app.main(["--headless", "--dry-run"]) == 1
    (service,) = RecordingService.instances
    assert RecordingService.events == ["function_keys", "trigger.start", "trigger.stop", "controller.stop"]
    assert service.started_with.trigger_enabled is True
    assert service.announce is app._print_trigger
    assert service.log.events() == [] and service.log._path == program.directory / "trigger.log"


def test_headless_announce_prints_one_plain_line(capsys):
    at = time.mktime((2026, 1, 2, 13, 4, 5, 0, 0, -1))
    app._print_trigger(TriggerEvent(at, 57, "ok"))
    assert capsys.readouterr().out == "Trigger accepted: F57 at 13:04:05\n"


def test_tray_mode_starts_the_service_before_the_loop_and_stops_it_after(program, monkeypatch):
    seen = {}

    class FakeTray:
        def __init__(self, controller, dictionaries, store, start_hidden=False, hook_error=None, **kwargs):
            seen.update(kwargs)

        def on_error(self, message):
            pass

        def run(self):
            RecordingService.events.append("tray.run")
            raise KeyboardInterrupt  # the loop ends badly: the service must still be stopped

    monkeypatch.setattr(tray_mod, "TrayApp", FakeTray)
    with pytest.raises(KeyboardInterrupt):
        app.main(["--dry-run"])
    (service,) = RecordingService.instances
    assert RecordingService.events == ["trigger.start", "tray.run", "trigger.stop", "controller.stop"]
    assert seen["trigger"] is service and seen["trigger_log"] is service.log
    assert isinstance(service.log, tray_mod.ToastingLog)
    assert service.log._path == program.directory / "trigger.log"
    assert service.started_with.trigger_enabled is True
