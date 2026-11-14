"""External macro trigger over a real named pipe (Windows only). Unique pipe names, a fake macro callback, no typing."""

from __future__ import annotations

import base64
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from mourse_decoder import trigger
from mourse_decoder import trigger_pipe as tp
from mourse_decoder.trigger import RateLimiter, TriggerLog, handle_request

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows only")

GOOD = b"MOURSE1 TRIGGER F57\n"
# The one-liner from the README; the test swaps the pipe name for a test pipe.
ONE_LINER = (
    "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
    "$p=[IO.Pipes.NamedPipeClientStream]::new('.',\"MourseDecoder-$sid\",'InOut'); "
    "$p.Connect(2000); $w=[IO.StreamWriter]::new($p); $w.NewLine=\"`n\"; "
    "$w.WriteLine('MOURSE1 TRIGGER F57'); $w.Flush(); [IO.StreamReader]::new($p).ReadLine()"
)


def unique_name() -> str:
    return "\\\\.\\pipe\\MourseDecoderTest-" + uuid.uuid4().hex


class Harness:
    """A PipeServer with the real request handling and a fake ``run``."""

    def __init__(self, code: str = "ok") -> None:
        self.name = unique_name()
        self.code = code
        self.raws: list[bytes] = []
        self.slots: list[int] = []
        self.sid = tp.current_user_sid()
        self.server = tp.PipeServer(self.name, trigger.pipe_sddl(self.sid), self.handle)

    def run(self, slot: int) -> str:
        self.slots.append(slot)
        return self.code

    def handle(self, raw: bytes) -> bytes:
        self.raws.append(raw)
        return handle_request(
            raw, run=self.run, limiter=RateLimiter(limit=1000), log=TriggerLog(), enabled=lambda: True,
            blocked=lambda: False,
        )


@pytest.fixture
def harness():
    h = Harness()
    try:
        assert h.server.start() is True
        yield h
    finally:
        h.server.stop()


def open_client(name: str):
    """A raw client handle (connected, nothing sent); the caller closes it."""
    kernel32 = tp._api().kernel32
    handle = kernel32.CreateFileW(name, 0xC0000000, 0, None, 3, 0x00100000, None)
    assert handle not in (None, tp._api().invalid), ctypes.WinError(ctypes.get_last_error())
    return handle


def close(handle) -> None:
    tp._api().kernel32.CloseHandle(handle)


def test_valid_request_gets_ok_and_runs_the_slot_once(harness):
    assert tp.send_request(harness.name, GOOD) == b"OK\n"
    assert harness.slots == [57] and harness.raws == [GOOD]


def test_error_codes_travel_back(harness):
    harness.code = "not_allowed"
    assert tp.send_request(harness.name, GOOD) == b"ERR not_allowed\n"


@pytest.mark.parametrize("line", [b"MOURSE1 TRIGGER F0\n", b"hello\n", b"MOURSE1 TRIGGER F5\r\n", b"MOURSE1 TRIGGER F5\nF6\n"])
def test_malformed_request_is_refused_and_runs_nothing(harness, line):
    assert tp.send_request(harness.name, line) == b"ERR bad_request\n"
    assert harness.slots == []


def test_oversized_request_is_refused_and_the_server_goes_on(harness):
    long_line = b"MOURSE1 TRIGGER F5" + b" " * 200 + b"\n"
    assert tp.send_request(harness.name, long_line) == b"ERR bad_request\n"
    assert harness.slots == []
    assert len(harness.raws[0]) <= trigger.MAX_REQUEST_BYTES + 1  # the server never buffers more than the cap + 1
    assert tp.send_request(harness.name, GOOD) == b"OK\n"


def test_request_without_newline_is_dropped_after_the_timeout(harness):
    from ctypes import wintypes

    client = open_client(harness.name)
    try:
        written = wintypes.DWORD()
        tp._api().kernel32.WriteFile(client, b"MOURSE1 TRIGGER F5", 18, ctypes.byref(written), None)
        started = time.monotonic()
        assert tp.send_request(harness.name, GOOD, timeout_s=6) == b"OK\n"
        assert 1.5 < time.monotonic() - started < 5
    finally:
        close(client)
    assert harness.slots == [57]  # only the second client ran a macro


def test_silent_client_is_dropped_after_about_two_seconds(harness):
    client = open_client(harness.name)
    try:
        started = time.monotonic()
        assert tp.send_request(harness.name, GOOD, timeout_s=6) == b"OK\n"
        assert 1.5 < time.monotonic() - started < 5
    finally:
        close(client)
    assert harness.raws == [GOOD]  # the silent client was not a request


def test_client_that_leaves_early_does_not_stop_the_server(harness):
    close(open_client(harness.name))
    assert tp.send_request(harness.name, GOOD) == b"OK\n"


def test_client_that_never_reads_the_reply_does_not_block_the_next_one(harness):
    from ctypes import wintypes

    client = open_client(harness.name)
    try:
        written = wintypes.DWORD()
        tp._api().kernel32.WriteFile(client, GOOD, len(GOOD), ctypes.byref(written), None)
        assert tp.send_request(harness.name, GOOD, timeout_s=6) == b"OK\n"
    finally:
        close(client)
    assert harness.slots == [57, 57]


def test_handler_exception_answers_broken(harness):
    def boom(raw: bytes) -> bytes:
        raise RuntimeError("macro text in here")

    harness.server._handler = boom
    assert tp.send_request(harness.name, GOOD) == b"ERR broken\n"
    harness.server._handler = harness.handle
    assert tp.send_request(harness.name, GOOD) == b"OK\n"


def test_second_server_on_the_same_name_does_not_start(harness):
    other = tp.PipeServer(harness.name, trigger.pipe_sddl(harness.sid), lambda raw: b"OK\n")
    started = time.monotonic()
    assert other.start() is False
    assert time.monotonic() - started < 1  # a foreign owner is not waited for
    other.stop()  # harmless
    assert tp.send_request(harness.name, GOOD) == b"OK\n"
    assert harness.raws == [GOOD]


def test_name_stays_taken_between_clients(harness):
    for _ in range(3):
        tp.send_request(harness.name, GOOD)
        assert tp.PipeServer(harness.name, trigger.pipe_sddl(harness.sid), lambda raw: b"OK\n").start() is False


def test_stop_unblocks_the_server_and_frees_the_name():
    h = Harness()
    assert h.server.start()
    assert tp.send_request(h.name, GOOD) == b"OK\n"
    started = time.monotonic()
    h.server.stop()
    assert time.monotonic() - started < 2
    with pytest.raises(FileNotFoundError):
        tp.send_request(h.name, GOOD)
    h.server.stop()  # idempotent
    again = tp.PipeServer(h.name, trigger.pipe_sddl(h.sid), h.handle)
    try:
        assert again.start() is True
    finally:
        again.stop()


def test_stop_while_a_client_is_connected_and_silent():
    h = Harness()
    assert h.server.start()
    client = open_client(h.name)
    try:
        time.sleep(0.2)
        started = time.monotonic()
        h.server.stop()
        assert time.monotonic() - started < 2.5
    finally:
        close(client)
    with pytest.raises(FileNotFoundError):
        tp.send_request(h.name, GOOD)


def fake_api(**functions) -> SimpleNamespace:
    return SimpleNamespace(kernel32=SimpleNamespace(**functions))


def test_deadline_cancels_again_until_the_block_is_left(monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "_api", lambda: fake_api(CancelIoEx=lambda handle, overlapped: calls.append(handle) or True))
    with tp._Deadline(5, 0.05) as deadline:
        time.sleep(0.4)
    assert deadline.expired and len(calls) >= 3  # a read that starts after the first cancel is hit by a later one
    count = len(calls)
    time.sleep(0.2)
    assert len(calls) == count


def test_deadline_does_nothing_after_the_block_was_left(monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "_api", lambda: fake_api(CancelIoEx=lambda handle, overlapped: calls.append(handle) or True))
    with tp._Deadline(5, 60) as deadline:
        pass
    deadline._fire()  # a timer thread that was already past its check
    assert calls == [] and deadline.expired is False


def test_leaving_the_block_waits_for_a_cancel_in_progress(monkeypatch):
    entered, release, left = threading.Event(), threading.Event(), threading.Event()

    def slow_cancel(handle, overlapped):
        entered.set()
        release.wait(5)
        return True

    monkeypatch.setattr(tp, "_api", lambda: fake_api(CancelIoEx=slow_cancel))
    deadline = tp._Deadline(5, 0.01)
    deadline.__enter__()
    assert entered.wait(5)
    leaver = threading.Thread(target=lambda: (deadline.__exit__(None, None, None), left.set()), daemon=True)
    leaver.start()
    try:
        assert not left.wait(0.3)  # still inside the cancel
    finally:
        release.set()
    assert left.wait(5)


def test_read_line_gives_up_at_the_deadline_even_between_reads(monkeypatch):
    def read(handle, buffer, size, count, overlapped):  # one byte per call, slower than the deadline allows for 40
        time.sleep(0.03)
        ctypes.memmove(buffer, b"x", 1)
        count._obj.value = 1
        return 1

    monkeypatch.setattr(tp, "_api", lambda: fake_api(ReadFile=read, CancelIoEx=lambda handle, overlapped: True))
    started = time.monotonic()
    raw, timed_out = tp._read_line(5, 40, 0.1)
    assert timed_out and 0 < len(raw) < 41
    assert time.monotonic() - started < 0.8


def test_handle_is_cleared_when_the_thread_ends():
    h = Harness()
    assert h.server.start()
    thread = h.server._thread
    assert h.server._handle is not None
    h.server.stop()
    thread.join(5)
    assert not thread.is_alive() and h.server._handle is None


def test_failed_thread_start_closes_the_pipe_and_raises(monkeypatch):
    def refuse(self):
        raise RuntimeError("can't start new thread")

    h = Harness()
    with monkeypatch.context() as patched:
        patched.setattr(tp.threading.Thread, "start", refuse)
        with pytest.raises(RuntimeError):
            h.server.start()
    assert h.server._handle is None and h.server._thread is None
    again = tp.PipeServer(h.name, trigger.pipe_sddl(h.sid), h.handle)
    try:
        assert again.start() is True  # the name is not held by a leaked handle
    finally:
        again.stop()


def stuck_first_server(monkeypatch):
    """A server whose handler hangs, stopped while it hangs; returns (name, sddl, release, client thread, start time)."""
    monkeypatch.setattr(tp, "_STOP_WAIT_S", 0.2)
    entered, release = threading.Event(), threading.Event()

    def slow(raw: bytes) -> bytes:
        entered.set()
        release.wait(10)
        return b"OK\n"

    name, sddl = unique_name(), trigger.pipe_sddl(tp.current_user_sid())
    first = tp.PipeServer(name, sddl, slow)
    assert first.start()
    client = threading.Thread(target=lambda: _try(tp.send_request, name, GOOD, 8), daemon=True)
    client.start()
    assert entered.wait(5)
    started = time.monotonic()
    first.stop()  # gives up after 0.2 s: the thread is busy in the handler and still owns the name
    assert 0.15 < time.monotonic() - started < 1.5
    return name, sddl, release, client, started


def _try(function, *args) -> None:
    try:
        function(*args)
    except OSError:
        pass


def test_start_waits_for_a_server_that_is_still_stopping(monkeypatch):
    name, sddl, release, client, started = stuck_first_server(monkeypatch)
    second = tp.PipeServer(name, sddl, lambda raw: b"OK\n")
    timer = threading.Timer(0.5, release.set)
    timer.start()
    try:
        assert second.start() is True
        assert time.monotonic() - started >= 0.5
        assert tp.send_request(name, GOOD) == b"OK\n"
    finally:
        release.set()
        timer.cancel()
        second.stop()
        client.join(10)


def test_start_gives_up_when_the_old_thread_does_not_end(monkeypatch):
    name, sddl, release, client, _ = stuck_first_server(monkeypatch)
    monkeypatch.setattr(tp, "_START_WAIT_S", 0.3)
    second = tp.PipeServer(name, sddl, lambda raw: b"OK\n")
    try:
        started = time.monotonic()
        assert second.start() is False
        assert time.monotonic() - started < 1.5
    finally:
        release.set()
        client.join(10)


def test_unusable_sddl_does_not_start():
    server = tp.PipeServer(unique_name(), "this is not an sddl", lambda raw: b"OK\n")
    assert server.start() is False
    server.stop()


def test_missing_pipe_is_file_not_found():
    with pytest.raises(FileNotFoundError):
        tp.send_request(unique_name(), GOOD)


def test_pipe_acl_is_the_documented_one(harness):
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetSecurityInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    dacl, descriptor, text = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    # SE_KERNEL_OBJECT = 6, DACL_SECURITY_INFORMATION = 4. A client handle: the server's own handle is busy in a
    # blocking ConnectNamedPipe and a security query on it would wait for that call.
    client = open_client(harness.name)
    try:
        assert advapi32.GetSecurityInfo(client, 6, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)) == 0
    finally:
        close(client)
    try:
        assert advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(text), None)
        actual = ctypes.wstring_at(text.value)
    finally:
        tp._api().kernel32.LocalFree(text)
        tp._api().kernel32.LocalFree(descriptor)
    # Windows maps the generic right GA to the pipe's file right FA when it stores the ACL, and prints well-known
    # SIDs as aliases when it reads it back (the built-in Administrator, RID 500, comes back as LA). So compare SIDs,
    # not text.
    protected, aces = parse_dacl(actual)
    assert protected
    assert [(kind, flags) for kind, flags, _, _ in aces] == [("D", ""), ("A", "")]
    assert [rights for _, _, rights, _ in aces] in (["FA", "FA"], ["GA", "GA"])
    assert [normalise_sid(sid) for *_, sid in aces] == ["S-1-5-2", normalise_sid(harness.sid)]  # deny network, allow this user


def parse_dacl(sddl: str) -> tuple[bool, list[tuple[str, str, str, str]]]:
    """A ``D:[P](type;flags;rights;object;inherited;sid)...`` string as (protected, [(type, flags, rights, sid)])."""
    match = re.fullmatch(r"D:(P?)((?:\([^()]*\))*)", sddl)
    assert match, sddl
    aces = []
    for body in re.findall(r"\(([^()]*)\)", match.group(2)):
        kind, flags, rights, _object, _inherited, sid = body.split(";")
        aces.append((kind, flags, rights, sid))
    return match.group(1) == "P", aces


def normalise_sid(sid: str) -> str:
    """``S-1-...`` form of a SID given as ``S-1-...`` or as an SDDL alias (LA, NU, WD, ...)."""
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    binary, text = ctypes.c_void_p(), ctypes.c_void_p()
    assert advapi32.ConvertStringSidToSidW(sid, ctypes.byref(binary)), f"not a SID: {sid!r}"
    try:
        assert advapi32.ConvertSidToStringSidW(binary, ctypes.byref(text))
        try:
            return ctypes.wstring_at(text.value)
        finally:
            tp._api().kernel32.LocalFree(text)
    finally:
        tp._api().kernel32.LocalFree(binary)


def test_acl_parser_and_sid_normaliser_read_aliases():
    protected, aces = parse_dacl("D:P(D;;FA;;;NU)(A;;FA;;;LA)")
    assert protected
    assert aces == [("D", "", "FA", "NU"), ("A", "", "FA", "LA")]
    assert normalise_sid("NU") == "S-1-5-2"
    assert normalise_sid("WD") == "S-1-1-0"
    assert normalise_sid("SY") == "S-1-5-18"
    assert normalise_sid("BA") == "S-1-5-32-544"
    assert normalise_sid("LA").startswith("S-1-5-21-") and normalise_sid("LA").endswith("-500")
    assert normalise_sid("S-1-5-2") == "S-1-5-2"
    assert parse_dacl("D:(A;;GA;;;WD)")[0] is False
    with pytest.raises(AssertionError):
        normalise_sid("not a sid")


def test_remote_clients_are_rejected_by_the_pipe_mode(monkeypatch):
    kernel32 = tp._api().kernel32
    original, seen = kernel32.CreateNamedPipeW, []

    def spy(*args):
        seen.append(args)
        return original(*args)

    monkeypatch.setattr(kernel32, "CreateNamedPipeW", spy)
    h = Harness()
    try:
        assert h.server.start()
    finally:
        h.server.stop()
    (name, open_mode, pipe_mode, max_instances, *_), = seen
    assert name == h.name
    assert pipe_mode & 0x8  # PIPE_REJECT_REMOTE_CLIENTS
    assert pipe_mode & 0x7 == 0  # byte pipe, byte reads, blocking
    assert open_mode & 0x00080000  # FILE_FLAG_FIRST_PIPE_INSTANCE
    assert max_instances == 1


def test_current_user_sid_is_the_one_the_process_runs_as():
    sid = tp.current_user_sid()
    assert sid.startswith("S-1-5-")
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30).stdout
    assert sid in out


def test_all_signatures_are_set():
    api = tp._api()
    for function in (
        api.kernel32.CreateNamedPipeW, api.kernel32.ConnectNamedPipe, api.kernel32.DisconnectNamedPipe,
        api.kernel32.ReadFile, api.kernel32.WriteFile, api.kernel32.FlushFileBuffers, api.kernel32.CancelIoEx,
        api.kernel32.CloseHandle, api.kernel32.CreateFileW, api.kernel32.WaitNamedPipeW, api.kernel32.LocalFree,
        api.advapi32.OpenProcessToken, api.advapi32.GetTokenInformation, api.advapi32.ConvertSidToStringSidW,
        api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW,
    ):
        assert function.argtypes, function.__name__
    assert api.kernel32.CreateNamedPipeW.restype is ctypes.c_void_p


# --- client_main -------------------------------------------------------------------------------------------------


def run_client(monkeypatch, capsys, reply=None, error=None, text="F57"):
    sent = []

    def fake_send(name, line, timeout_s=2.0):
        sent.append((name, line))
        if error:
            raise error
        return reply

    monkeypatch.setattr(tp, "send_request", fake_send)
    monkeypatch.setattr(tp, "current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    code = tp.client_main(text)
    return code, capsys.readouterr().out, sent


@pytest.mark.parametrize(
    ("reply", "error", "exit_code", "output"),
    [
        (b"OK\n", None, 0, "ok"),
        (None, FileNotFoundError(), 10, "error pipe_not_found"),
        (None, PermissionError(), 11, "error access_denied"),
        (b"ERR unknown_macro\n", None, 12, "error unknown_macro"),
        (b"ERR not_allowed\n", None, 12, "error not_allowed"),
        (b"ERR busy\n", None, 13, "error busy"),
        (b"ERR rate_limited\n", None, 14, "error rate_limited"),
        (b"ERR inactive\n", None, 15, "error inactive"),
        (b"ERR bad_request\n", None, 16, "error bad_request"),
        (b"ERR broken\n", None, 16, "error broken"),
        (b"garbage\n", None, 16, "error bad_request"),
        (None, ConnectionError("closed"), 16, "error protocol"),
        (None, TimeoutError(), 17, "error timeout"),
    ],
)
def test_client_exit_codes(monkeypatch, capsys, reply, error, exit_code, output):
    code, out, sent = run_client(monkeypatch, capsys, reply, error)
    assert (code, out.strip()) == (exit_code, output)
    assert sent == [("\\\\.\\pipe\\MourseDecoder-S-1-5-21-1-2-3-1001", b"MOURSE1 TRIGGER F57\n")]


@pytest.mark.parametrize("text", ["", "57", "F0", "F101", "F01", "f5", "F5 ", " F5", "F5\n", "F-1", "F1000", "F5;x", "F٥"])
def test_client_refuses_a_bad_id_without_connecting(monkeypatch, capsys, text):
    code, out, sent = run_client(monkeypatch, capsys, b"OK\n", None, text)
    assert code == 2 and out.startswith("error bad_id") and sent == []


@pytest.mark.parametrize("text", ["F1", "F100"])
def test_client_accepts_the_ends_of_the_range(monkeypatch, capsys, text):
    code, _, sent = run_client(monkeypatch, capsys, b"OK\n", None, text)
    assert code == 0 and sent[0][1] == f"MOURSE1 TRIGGER {text}\n".encode()


def test_client_end_to_end_over_a_real_pipe(harness, monkeypatch, capsys):
    monkeypatch.setattr(trigger, "pipe_name", lambda sid: harness.name)
    assert tp.client_main("F57") == 0
    assert capsys.readouterr().out.strip() == "ok"
    assert harness.slots == [57]
    harness.code = "busy"
    assert tp.client_main("F3") == 13
    assert capsys.readouterr().out.strip() == "error busy"


def test_client_without_a_server(monkeypatch, capsys):
    monkeypatch.setattr(trigger, "pipe_name", lambda sid: unique_name())
    assert tp.client_main("F5") == 10
    assert capsys.readouterr().out.strip() == "error pipe_not_found"


def test_client_with_a_server_that_never_answers(monkeypatch, capsys):
    name = unique_name()
    monkeypatch.setattr(trigger, "pipe_name", lambda sid: name)

    def silent(raw: bytes) -> bytes:  # answers after the client's timeout (the server-side flush is cancelled)
        time.sleep(2.5)
        return b"OK\n"

    server = tp.PipeServer(name, trigger.pipe_sddl(tp.current_user_sid()), silent)
    assert server.start()
    try:
        started = time.monotonic()
        assert tp.client_main("F5") == 17
        assert time.monotonic() - started < 4
    finally:
        server.stop()
    assert capsys.readouterr().out.strip() == "error timeout"


def test_attach_console_is_skipped_when_stdout_exists(monkeypatch):
    called = []
    monkeypatch.setattr(tp, "_api", lambda: called.append(1))
    tp._attach_console()
    assert called == []


def test_attach_console_never_raises_without_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(tp, "_api", lambda: (_ for _ in ()).throw(OSError("no console")))
    tp._attach_console()
    tp._say("still fine")  # no stdout: prints nothing, raises nothing


def test_client_path_does_not_load_the_gui():
    # mourse_decoder/__init__ imports settings, which imports _native; so the compiled module is loaded, no engine starts
    code = (
        "import sys; import mourse_decoder.trigger_pipe as t; "
        "bad = [m for m in ('tkinter', 'pystray', 'PIL', 'mourse_decoder.ui', 'mourse_decoder.app') if m in sys.modules]; "
        "sys.exit(1 if bad else 0)"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(trigger.__file__).parent.parent)}
    done = subprocess.run([sys.executable, "-c", code], env=env, timeout=60, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="no powershell.exe")
def test_documented_powershell_one_liner(harness):
    script = ONE_LINER.replace('"MourseDecoder-$sid"', "'" + harness.name.rsplit("\\", 1)[1] + "'")
    assert harness.name.rsplit("\\", 1)[1] in script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=60,
    )
    assert done.stdout.strip() == "OK", done.stderr
    assert harness.raws == [GOOD]
    assert harness.slots == [57]
