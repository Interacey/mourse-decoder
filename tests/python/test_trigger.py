"""External macro trigger: protocol, rate limit, request handling, log and service (no pipe, no typing)."""

from __future__ import annotations

import logging
import threading

import pytest
from mourse_decoder import trigger
from mourse_decoder import trigger_pipe as tp
from mourse_decoder.trigger import (
    CODES,
    BadRequest,
    RateLimiter,
    TriggerLog,
    TriggerService,
    format_response,
    handle_request,
    parse_request,
    parse_response,
    pipe_name,
    pipe_sddl,
)

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
GOOD = b"MOURSE1 TRIGGER F57\n"


# --- parse_request ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "slot"), [(b"MOURSE1 TRIGGER F1\n", 1), (GOOD, 57), (b"MOURSE1 TRIGGER F100\n", 100)])
def test_parse_request_accepts_the_one_form(raw, slot):
    assert parse_request(raw) == slot


@pytest.mark.parametrize(
    "raw",
    [
        b"MOURSE1 TRIGGER F0\n",
        b"MOURSE1 TRIGGER F101\n",
        b"MOURSE1 TRIGGER F01\n",
        b"MOURSE1 TRIGGER f5\n",
        b"MOURSE1 TRIGGER F5",
        b"MOURSE1 TRIGGER F5 \n",
        b"MOURSE1 TRIGGER F5\r\n",
        b"MOURSE1 TRIGGER F5\x00\n",
        b"MOURSE1 TRIGGER F5\n\x00",
        "MOURSE1 TRIGGER F5ä\n".encode(),
        b"MOURSE1 TRIGGER F5\nMOURSE1 TRIGGER F6\n",
        b"MOURSE1 TRIGGER F5\n\n",
        b"MOURSE1 TRIGGER  F5\n",
        b"MOURSE2 TRIGGER F5\n",
        b"MOURSE1 TRIGGER F\n",
        b"MOURSE1 TRIGGER F-1\n",
        b"MOURSE1 TRIGGER F5.0\n",
        b"MOURSE1 TYPE hello\n",
        b"",
        b"\n",
        # 65 bytes, otherwise a valid request: the length cap alone must refuse it
        b"MOURSE1 TRIGGER F" + b"1" + b"\n" + b" " * 46,
    ],
)
def test_parse_request_refuses_everything_else(raw):
    with pytest.raises(BadRequest):
        parse_request(raw)


def test_length_cap_is_checked_on_its_own():
    raw = b"MOURSE1 TRIGGER F5\n" + b"x" * (trigger.MAX_REQUEST_BYTES - len(GOOD) + 2)
    assert len(raw) == trigger.MAX_REQUEST_BYTES + 1
    with pytest.raises(BadRequest, match="too long"):
        parse_request(raw)


def test_bad_request_is_a_value_error():
    assert issubclass(BadRequest, ValueError)


# --- responses ---------------------------------------------------------------------------------------------------


def test_format_response():
    assert format_response("ok") == b"OK\n"
    assert format_response("busy") == b"ERR busy\n"
    with pytest.raises(ValueError):
        format_response("F5 hello")


@pytest.mark.parametrize("code", CODES)
def test_response_round_trip(code):
    assert parse_response(format_response(code)) == code


@pytest.mark.parametrize("raw", [b"", b"hello\n", b"OK", b"OK\r\n", b"ERR \n", b"ERR nonsense\n", b"ERR ok\n", b"\xff\n"])
def test_unknown_reply_is_bad_request(raw):
    assert parse_response(raw) == "bad_request"


# --- pipe name and ACL -------------------------------------------------------------------------------------------


def test_pipe_name_and_sddl():
    assert pipe_name(SID) == "\\\\.\\pipe\\MourseDecoder-" + SID
    assert pipe_sddl(SID) == f"D:P(D;;GA;;;NU)(A;;GA;;;{SID})"


@pytest.mark.parametrize("sid", ["", "S-1-5", "S-1-5-abc", "x" + SID, SID + ")", SID + "\n", "S-1-5-21-1;(A;;GA;;;WD)", "S-2-5-21-1"])
def test_malformed_sid_is_refused(sid):
    with pytest.raises(ValueError):
        pipe_name(sid)
    with pytest.raises(ValueError):
        pipe_sddl(sid)


# --- rate limit --------------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_rate_limit_allows_ten_per_window():
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    assert [limiter.allow() for _ in range(10)] == [True] * 10
    assert limiter.allow() is False
    clock.now += 10.5
    assert limiter.allow() is True


def test_rejected_requests_count_against_the_window():
    clock = Clock()
    limiter = RateLimiter(limit=3, window_s=10.0, clock=clock)
    assert limiter.allow() and limiter.allow()
    clock.now += 5
    assert limiter.allow()  # 3rd
    clock.now += 1
    assert limiter.allow() is False
    clock.now += 1
    assert limiter.allow() is False
    clock.now += 3.5  # 10.5 s: the first two are out; the accepted one and both rejected ones are in the window
    assert limiter.allow() is False  # a limiter that forgot the rejected ones would say True here
    clock.now += 6
    assert limiter.allow() is True


def test_rate_limit_is_a_sliding_window():
    clock = Clock()
    limiter = RateLimiter(limit=3, window_s=10.0, clock=clock)
    assert limiter.allow() and limiter.allow()
    clock.now += 6
    assert limiter.allow()
    assert limiter.allow() is False  # 4th within 10 s
    clock.now += 4.5  # the first two are 10.5 s old: the window holds the 3rd, the rejected one and this request
    assert limiter.allow() is True
    assert limiter.allow() is False


def test_rate_limiter_is_thread_safe():
    limiter = RateLimiter(limit=50, window_s=1000.0)
    results: list[bool] = []

    def hit() -> None:
        results.extend(limiter.allow() for _ in range(25))

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(results) == 200 and results.count(True) == 50


# --- handle_request ----------------------------------------------------------------------------------------------


class Calls:
    """A fake ``run`` that records the slots and answers with ``code`` (or raises ``error``)."""

    def __init__(self, code: str = "ok", error: Exception | None = None) -> None:
        self.code, self.error, self.slots = code, error, []

    def __call__(self, slot: int) -> str:
        self.slots.append(slot)
        if self.error:
            raise self.error
        return self.code


def ask(raw=GOOD, run=None, limiter=None, log=None, enabled=True, blocked=False):
    run = run or Calls()
    log = log or TriggerLog()
    reply = handle_request(
        raw, run=run, limiter=limiter or RateLimiter(), log=log, enabled=lambda: enabled, blocked=lambda: blocked
    )
    return reply, run, log


def test_valid_request_runs_the_slot():
    reply, run, log = ask()
    assert reply == b"OK\n" and run.slots == [57]
    assert [(e.slot, e.code) for e in log.events()] == [(57, "ok")]


@pytest.mark.parametrize("code", ["unknown_macro", "not_allowed", "inactive", "busy", "broken"])
def test_run_codes_are_passed_on(code):
    reply, run, log = ask(run=Calls(code))
    assert reply == f"ERR {code}\n".encode() and run.slots == [57]
    assert log.events()[-1].code == code


def test_unknown_code_from_run_is_broken():
    reply, _, log = ask(run=Calls("Das Makro sagt hallo"))
    assert reply == b"ERR broken\n" and log.events()[-1].code == "broken"


def test_exception_text_never_reaches_the_caller():
    secret = "macro text with a password"
    reply, _, log = ask(run=Calls(error=RuntimeError(secret)))
    assert reply == b"ERR broken\n" and secret.encode() not in reply
    assert secret not in repr(log.events())


def test_disabled_setting_answers_inactive_and_runs_nothing():
    reply, run, log = ask(enabled=False)
    assert reply == b"ERR inactive\n" and run.slots == []
    assert log.events()[-1].code == "inactive"


def test_import_gate_blocks_with_busy():
    reply, run, log = ask(blocked=True)
    assert reply == b"ERR busy\n" and run.slots == []
    assert [(e.slot, e.code) for e in log.events()] == [(57, "busy")]


@pytest.mark.parametrize("raw", [b"", b"MOURSE1 TRIGGER F0\n", b"MOURSE1 TRIGGER F5", b"x" * 500, b"MOURSE1 TRIGGER F5\nF6\n"])
def test_bad_request_is_answered_and_logged_without_a_slot(raw):
    reply, run, log = ask(raw)
    assert reply == b"ERR bad_request\n" and run.slots == []
    assert [(e.slot, e.code) for e in log.events()] == [(None, "bad_request")]


def test_rate_limit_comes_first_and_every_request_counts():
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    log, run = TriggerLog(), Calls()
    for _ in range(10):
        ask(run=run, limiter=limiter, log=log)
    assert len(run.slots) == 10
    for raw in (GOOD, b"garbage", b""):  # a valid, an unparseable and an empty request: all limited, none run
        reply, _, _ = ask(raw, run=run, limiter=limiter, log=log)
        assert reply == b"ERR rate_limited\n"
    assert len(run.slots) == 10
    assert [e.code for e in log.events()][-3:] == ["rate_limited"] * 3


def test_rate_limit_also_counts_when_disabled_or_blocked():
    limiter = RateLimiter(limit=2)
    ask(limiter=limiter, enabled=False)
    ask(limiter=limiter, blocked=True)
    reply, run, _ = ask(limiter=limiter)
    assert reply == b"ERR rate_limited\n" and run.slots == []


def test_reply_is_always_one_of_the_fixed_lines():
    for raw in (GOOD, b"MOURSE1 TRIGGER F99\n; type secret\n", b"\xff" * 10):
        for code in (*CODES, "free text"):
            reply, _, _ = ask(raw, run=Calls(code))
            assert reply in {b"OK\n", *(f"ERR {c}\n".encode() for c in CODES)}


# --- TriggerLog --------------------------------------------------------------------------------------------------


def test_log_keeps_the_last_100_events_newest_last():
    log = TriggerLog()
    for number in range(1, 151):
        log.add(number % 100 + 1, "ok", now=float(number))
    events = log.events()
    assert len(events) == 100
    assert events[0].time == 51.0 and events[-1].time == 150.0


def test_event_is_frozen():
    event = TriggerLog().add(3, "ok")
    with pytest.raises(AttributeError):
        event.code = "busy"  # type: ignore[misc]


def test_log_file_has_one_line_per_event(tmp_path):
    path = tmp_path / "trigger.log"
    log = TriggerLog(path=path)
    log.add(57, "ok", now=0.0)
    log.add(None, "bad_request", now=0.0)
    lines = path.read_text(encoding="ascii").splitlines()
    assert len(lines) == 2
    time_part, slot, code = lines[0].split(" ")
    assert (slot, code) == ("F57", "ok") and len(time_part) == 19 and time_part[10] == "T"
    assert lines[1].split(" ")[1:] == ["F-", "bad_request"]


def test_log_file_rotates_once(tmp_path):
    path = tmp_path / "trigger.log"
    log = TriggerLog(path=path, max_bytes=100)
    for _ in range(30):
        log.add(5, "ok", now=0.0)
    rotated = tmp_path / "trigger.log.1"
    assert rotated.exists() and path.stat().st_size <= 100 + 40
    assert not (tmp_path / "trigger.log.2").exists()
    assert len(list(tmp_path.iterdir())) == 2


def test_log_survives_io_errors(tmp_path):
    log = TriggerLog(path=tmp_path / "no" / "such" / "dir" / "trigger.log")
    event = log.add(5, "ok")
    assert log.events() == [event]


def test_log_survives_a_file_that_is_a_folder(tmp_path):
    (tmp_path / "trigger.log").mkdir()
    log = TriggerLog(path=tmp_path / "trigger.log")
    log.add(5, "ok")
    assert len(log.events()) == 1


def test_refused_flood_is_capped_and_keeps_the_older_history():
    log = TriggerLog()
    for number in range(5):
        log.add(7, "ok", now=float(number))
    for _ in range(100):
        log.add(None, "bad_request", now=10.0)
    codes = [e.code for e in log.events()]
    assert codes.count("ok") == 5 and codes.count("bad_request") == 20
    log.add(7, "ok", now=11.0)  # accepted triggers are never suppressed
    assert log.events()[-1].code == "ok" and len(log.events()) == 26


def test_flood_is_summarised_once_per_window_in_the_file(tmp_path):
    clock = Clock()
    path = tmp_path / "trigger.log"
    log = TriggerLog(path=path, clock=clock)
    for _ in range(100):
        log.add(None, "rate_limited", now=0.0)
    lines = path.read_text(encoding="ascii").splitlines()
    assert len(lines) == 20  # the summary waits for the end of the window
    clock.now += 10.5
    log.add(5, "ok", now=0.0)
    lines = path.read_text(encoding="ascii").splitlines()
    assert [line.split(" ", 1)[1] for line in lines[-2:]] == ["F- suppressed 80", "F5 ok"]
    assert len(lines) == 22
    clock.now += 10.5  # the new window starts clean: nothing was suppressed in the last one
    log.add(5, "ok", now=0.0)
    assert len(path.read_text(encoding="ascii").splitlines()) == 23


def test_flood_in_the_next_window_is_logged_again():
    clock = Clock()
    log = TriggerLog(clock=clock)
    for _ in range(30):
        log.add(None, "bad_request")
    clock.now += 10.5
    for _ in range(30):
        log.add(None, "bad_request")
    assert len(log.events()) == 40


def test_failed_rotation_truncates_the_file_and_warns_once(tmp_path, monkeypatch, caplog):
    def refuse(source, target):
        raise PermissionError("trigger.log.1 is open elsewhere")

    path = tmp_path / "trigger.log"
    log = TriggerLog(path=path, max_bytes=100)
    with monkeypatch.context() as patched, caplog.at_level(logging.WARNING, logger="mourse_decoder"):
        patched.setattr(trigger.os, "replace", refuse)
        for _ in range(60):
            log.add(5, "ok", now=0.0)
    assert path.stat().st_size <= 100 + 40
    assert [r for r in caplog.records if "nicht rotiert" in r.getMessage()].__len__() == 1
    assert not [r for r in caplog.records if "nicht geschrieben" in r.getMessage()]
    assert len(log.events()) == 60


# --- TriggerService ----------------------------------------------------------------------------------------------


class FakeServer:
    created: list[FakeServer] = []

    def __init__(self, name, sddl, handler, ok=True) -> None:
        self.name, self.sddl, self.handler, self.ok = name, sddl, handler, ok
        self.started = self.stops = 0
        FakeServer.created.append(self)

    def start(self) -> bool:
        self.started += 1
        return self.ok

    def stop(self) -> None:
        self.stops += 1


class FakeSettings:
    def __init__(self, enabled) -> None:
        self.trigger_enabled = enabled


class FakeController:
    def __init__(self, code="ok") -> None:
        self.code, self.slots = code, []

    def trigger_slot(self, number: int) -> str:
        self.slots.append(number)
        return self.code


@pytest.fixture
def servers():
    FakeServer.created = []
    return FakeServer


def make_service(controller=None, announce=None, blocked=lambda: False, factory=FakeServer, log=None):
    return TriggerService(
        controller or FakeController(), log or TriggerLog(), blocked=blocked, announce=announce,
        server_factory=factory, sid=lambda: SID,
    )


def test_service_starts_only_when_enabled(servers):
    service = make_service()
    assert service.status == "off"
    service.start(FakeSettings(False))
    assert servers.created == [] and service.status == "off"
    service.start(FakeSettings(True))
    assert service.status == "running" and len(servers.created) == 1
    server = servers.created[0]
    assert (server.name, server.sddl, server.started) == (pipe_name(SID), pipe_sddl(SID), 1)


def test_settings_without_the_attribute_mean_off(servers):
    service = make_service()
    service.start(object())
    assert servers.created == [] and service.status == "off"


@pytest.mark.parametrize("value", [1, "yes", None])
def test_only_true_turns_it_on(servers, value):
    make_service().start(FakeSettings(value))
    assert servers.created == []


def test_apply_toggles(servers):
    service = make_service()
    service.apply(FakeSettings(True))
    service.apply(FakeSettings(True))  # unchanged: no second server
    assert len(servers.created) == 1 and servers.created[0].stops == 0
    service.apply(FakeSettings(False))
    assert servers.created[0].stops == 1 and service.status == "off"
    service.apply(FakeSettings(True))
    assert len(servers.created) == 2 and service.status == "running"


def test_stop_is_idempotent(servers):
    service = make_service()
    service.stop()
    service.start(FakeSettings(True))
    service.stop()
    service.stop()
    assert servers.created[0].stops == 1 and service.status == "off"


def test_unavailable_when_the_pipe_cannot_be_opened(servers):
    service = make_service(factory=lambda *args: FakeServer(*args, ok=False))
    service.start(FakeSettings(True))
    assert service.status == "unavailable"
    service.apply(FakeSettings(False))
    assert service.status == "off"


def test_unavailable_is_retried_on_the_next_apply(servers):
    answers = iter([False, True])
    service = make_service(factory=lambda *args: FakeServer(*args, ok=next(answers)))
    service.apply(FakeSettings(True))
    assert service.status == "unavailable"
    service.apply(FakeSettings(True))
    assert service.status == "running"


def test_unavailable_when_the_sid_cannot_be_read(servers):
    def no_sid() -> str:
        raise OSError("no token")

    service = TriggerService(FakeController(), TriggerLog(), blocked=lambda: False, server_factory=FakeServer, sid=no_sid)
    service.start(FakeSettings(True))
    assert service.status == "unavailable" and servers.created == []


def test_service_does_nothing_off_windows(monkeypatch, servers):
    monkeypatch.setattr(trigger.sys, "platform", "linux")
    service = TriggerService(FakeController(), TriggerLog(), blocked=lambda: False)
    service.start(FakeSettings(True))
    assert service.status == "unavailable"
    service.stop()
    assert service.status == "off"


class ExplodingServer(FakeServer):
    def start(self) -> bool:
        raise RuntimeError("can't start new thread")


@pytest.mark.parametrize("where", ["start", "factory", "sid"])
def test_unexpected_error_while_opening_means_unavailable(servers, caplog, where):
    def broken_factory(*args):
        raise RuntimeError("factory failed")

    def broken_sid() -> str:
        raise RuntimeError("sid failed")

    factory = {"start": ExplodingServer, "factory": broken_factory, "sid": FakeServer}[where]
    service = TriggerService(
        FakeController(), TriggerLog(), blocked=lambda: False, server_factory=factory,
        sid=broken_sid if where == "sid" else (lambda: SID),
    )
    with caplog.at_level(logging.ERROR, logger="mourse_decoder"):
        service.start(FakeSettings(True))  # must not raise
    assert service.status == "unavailable"
    assert [r for r in caplog.records if r.exc_info]  # the cause is in the log
    service.apply(FakeSettings(False))
    assert service.status == "off"


@pytest.mark.parametrize("error", [AttributeError("no WinDLL"), OSError("no token"), ValueError("bad sid")])
def test_client_without_windows_pipe_api_exits_cleanly(monkeypatch, capsys, error):
    def no_api():
        raise error

    monkeypatch.setattr(tp, "_api", no_api)  # what a non-Windows interpreter does inside current_user_sid
    assert tp.client_main("F5") == trigger.EXIT_CODES["unsupported"] == 16
    captured = capsys.readouterr()
    assert captured.out.strip() == "error unsupported" and "Traceback" not in captured.err


def test_handler_runs_the_controller_and_logs(servers):
    controller, log = FakeController(), TriggerLog()
    service = make_service(controller, log=log)
    service.start(FakeSettings(True))
    assert servers.created[0].handler(GOOD) == b"OK\n"
    assert controller.slots == [57] and log.events()[-1].code == "ok"


def test_handler_refuses_while_the_service_is_off(servers):
    controller = FakeController()
    service = make_service(controller)
    service.start(FakeSettings(True))
    handler = servers.created[0].handler
    service.stop()
    assert handler(GOOD) == b"ERR inactive\n" and controller.slots == []


def test_handler_honours_the_import_gate(servers):
    controller = FakeController()
    service = make_service(controller, blocked=lambda: True)
    service.start(FakeSettings(True))
    assert servers.created[0].handler(GOOD) == b"ERR busy\n" and controller.slots == []


def test_default_gate_is_the_import_gate(servers):
    from mourse_decoder.import_gate import gate

    service = TriggerService(FakeController(), TriggerLog(), server_factory=FakeServer, sid=lambda: SID)
    service.start(FakeSettings(True))
    handler = servers.created[0].handler
    assert gate.begin()
    try:
        assert handler(GOOD) == b"ERR busy\n"
    finally:
        gate.end()
    assert handler(GOOD) == b"OK\n"


def test_announce_only_for_accepted_triggers(servers):
    announced = []
    controller = FakeController()
    service = make_service(controller, announce=announced.append)
    service.start(FakeSettings(True))
    handler = servers.created[0].handler
    handler(b"garbage")
    controller.code = "busy"
    handler(GOOD)
    assert announced == []
    controller.code = "ok"
    handler(GOOD)
    assert [(e.slot, e.code) for e in announced] == [(57, "ok")]


def test_broken_announce_does_not_break_the_reply(servers):
    def boom(event) -> None:
        raise RuntimeError("toast failed")

    service = make_service(announce=boom)
    service.start(FakeSettings(True))
    assert servers.created[0].handler(GOOD) == b"OK\n"


def test_module_imports_without_windows_code():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(trigger.__file__).read_text(encoding="utf-8"))
    top = {n.names[0].name.split(".")[0] for n in tree.body if isinstance(n, ast.Import)}
    assert not top & {"ctypes", "winreg", "msvcrt"}
