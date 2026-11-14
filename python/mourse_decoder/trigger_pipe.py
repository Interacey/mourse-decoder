"""Windows named pipe for the external macro trigger (ctypes only; imports on every system, works on Windows).

``PipeServer`` serves one client at a time on one pipe instance that is never closed between clients, so the name
cannot be taken over while the server runs. ``client_main`` is what ``MourseDecoder.exe --trigger F57`` runs; it
must stay light: no tkinter, no tray, no native module, no settings.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace

from . import trigger

_PIPE_ACCESS_DUPLEX = 3
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_REJECT_REMOTE_CLIENTS = 0x8
_PIPE_MODE = PIPE_REJECT_REMOTE_CLIENTS  # PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT are all 0
_GENERIC_READ_WRITE = 0xC0000000
_OPEN_EXISTING = 3
_SECURITY_SQOS_PRESENT = 0x00100000  # with SECURITY_ANONYMOUS (0): the server learns nothing about the caller
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 8
_TOKEN_USER = 1
_ATTACH_PARENT_PROCESS = 0xFFFFFFFF

_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5
_ERROR_SEM_TIMEOUT = 121
_ERROR_PIPE_BUSY = 231
_ERROR_NO_DATA = 232
_ERROR_PIPE_CONNECTED = 535

_BUFFER_BYTES = 512
_STOP_WAIT_S = 1.0  # the handler only waits for the controller lock; a longer wait would only block the UI thread
_START_WAIT_S = 2.5  # how long start() waits for a thread that stop() left running to give the name back
_lingering: dict[str, threading.Thread] = {}  # pipe name -> server thread that was still alive when stop() gave up
_ID = re.compile(r"F([1-9][0-9]{0,2})")


def _api() -> SimpleNamespace:
    """kernel32 and advapi32 with every signature set (handles are pointer sized, not int)."""
    api = getattr(_api, "cache", None)
    if api is not None:
        return api
    from ctypes import wintypes

    handle, dword, boolean, wstr = ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR
    pdword = ctypes.POINTER(dword)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    def sign(function, result, *args):
        function.restype, function.argtypes = result, list(args)

    sign(kernel32.CreateNamedPipeW, handle, wstr, dword, dword, dword, dword, dword, dword, handle)
    sign(kernel32.ConnectNamedPipe, boolean, handle, handle)
    sign(kernel32.DisconnectNamedPipe, boolean, handle)
    sign(kernel32.ReadFile, boolean, handle, handle, dword, pdword, handle)
    sign(kernel32.WriteFile, boolean, handle, handle, dword, pdword, handle)
    sign(kernel32.FlushFileBuffers, boolean, handle)
    sign(kernel32.CancelIoEx, boolean, handle, handle)
    sign(kernel32.CloseHandle, boolean, handle)
    sign(kernel32.CreateFileW, handle, wstr, dword, dword, handle, dword, dword, handle)
    sign(kernel32.WaitNamedPipeW, boolean, wstr, dword)
    sign(kernel32.GetCurrentProcess, handle)
    sign(kernel32.LocalFree, handle, handle)
    sign(kernel32.AttachConsole, boolean, dword)
    sign(advapi32.OpenProcessToken, boolean, handle, dword, ctypes.POINTER(handle))
    sign(advapi32.GetTokenInformation, boolean, handle, ctypes.c_int, handle, dword, pdword)
    sign(advapi32.ConvertSidToStringSidW, boolean, handle, ctypes.POINTER(handle))
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    sign(convert, boolean, wstr, dword, ctypes.POINTER(handle), pdword)

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", dword), ("lpSecurityDescriptor", handle), ("bInheritHandle", boolean)]

    api = SimpleNamespace(
        kernel32=kernel32, advapi32=advapi32, SecurityAttributes=SecurityAttributes,
        invalid=ctypes.c_void_p(-1).value,
    )
    _api.cache = api  # type: ignore[attr-defined]
    return api


def current_user_sid() -> str:
    """SID of the account this process runs as, e.g. ``S-1-5-21-...-1001``; OSError if Windows refuses."""
    api = _api()
    token = ctypes.c_void_p()
    if not api.advapi32.OpenProcessToken(api.kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        from ctypes import wintypes

        needed = wintypes.DWORD()
        api.advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not api.advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER starts with the SID pointer
        text = ctypes.c_void_p()
        if not api.advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text.value)
        finally:
            api.kernel32.LocalFree(text)
    finally:
        api.kernel32.CloseHandle(token)


class _Deadline:
    """Cancels the blocking I/O on ``handle`` after ``seconds``; ``expired`` tells whether it did.

    The cancel is repeated every 50 ms until the block is left, so a read that starts just after the first cancel
    is cancelled too. After the block is left nothing is cancelled any more (``__exit__`` waits for a cancel that
    is running), so a later call on the same handle is never hit.
    """

    def __init__(self, handle, seconds: float) -> None:
        self._handle = handle
        self.expired = False
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        while True:
            with self._lock:
                if self._done.is_set():
                    return
                self.expired = True
                _api().kernel32.CancelIoEx(self._handle, None)
            if self._done.wait(0.05):
                return

    def __enter__(self) -> _Deadline:
        self._timer.start()
        return self

    def __exit__(self, *exc) -> None:
        self._timer.cancel()
        with self._lock:
            self._done.set()


def _read_line(handle, cap: int, seconds: float) -> tuple[bytes, bool]:
    """Bytes up to and including the first newline, at most ``cap`` + 1 of them; (data, timed_out)."""
    from ctypes import wintypes

    kernel32, raw, count = _api().kernel32, b"", wintypes.DWORD()
    buffer = ctypes.create_string_buffer(cap + 1)
    with _Deadline(handle, seconds) as deadline:
        while len(raw) <= cap and b"\n" not in raw and not deadline.expired:
            if not kernel32.ReadFile(handle, buffer, cap + 1 - len(raw), ctypes.byref(count), None):
                break
            raw += buffer.raw[: count.value]
    return raw, deadline.expired


def _write(handle, data: bytes) -> bool:
    from ctypes import wintypes

    count = wintypes.DWORD()
    return bool(_api().kernel32.WriteFile(handle, data, len(data), ctypes.byref(count), None))


class PipeServer:
    """One daemon thread, one pipe instance, one request line per client."""

    def __init__(self, name: str, sddl: str, handler: Callable[[bytes], bytes]) -> None:
        self._name = name
        self._sddl = sddl
        self._handler = handler
        self._handle: int | None = None
        self._lock = threading.Lock()  # guards ``_handle`` between stop() and the thread that closes it
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> bool:
        """False if the pipe could not be created, e.g. another process already owns the name.

        If an earlier server of this process on the same name is still stopping, waits up to ``_START_WAIT_S`` for it.
        """
        if self._thread is not None:
            return True
        try:
            api = _api()
        except (AttributeError, OSError):
            return False
        descriptor = ctypes.c_void_p()
        if not api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            self._sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            return False
        try:
            attributes = api.SecurityAttributes(ctypes.sizeof(api.SecurityAttributes), descriptor, False)
            give_up = time.monotonic() + _START_WAIT_S
            while True:
                handle = api.kernel32.CreateNamedPipeW(
                    self._name, _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE, _PIPE_MODE, 1,
                    _BUFFER_BYTES, _BUFFER_BYTES, 0, ctypes.byref(attributes),
                )
                if handle is not None and handle != api.invalid:
                    break
                old = _lingering.get(self._name)
                if old is None or not old.is_alive() or time.monotonic() >= give_up:
                    return False
                old.join(0.05)
        finally:
            api.kernel32.LocalFree(descriptor)
        _lingering.pop(self._name, None)
        thread = threading.Thread(target=self._serve, args=(handle,), name="trigger-pipe", daemon=True)
        with self._lock:
            self._handle = handle
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._handle = None
            api.kernel32.CloseHandle(handle)
            raise
        self._thread = thread
        return True

    def stop(self) -> None:
        """Ends the thread (it closes the pipe); the name is free again afterwards. Safe to call twice.

        Gives up after ``_STOP_WAIT_S`` if a handler is still running; the thread then ends and closes the pipe
        when the handler returns, and a ``start`` on the same name waits for that.
        """
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stopping.set()
        kernel32 = _api().kernel32
        deadline = time.monotonic() + _STOP_WAIT_S
        while thread.is_alive() and time.monotonic() < deadline:
            with self._lock:  # the thread clears the handle under this lock before it closes it
                if self._handle is not None:
                    kernel32.CancelIoEx(self._handle, None)  # repeated: the thread may not be waiting yet
            thread.join(0.05)
        if thread.is_alive():
            _lingering[self._name] = thread

    def _serve(self, handle) -> None:
        kernel32 = _api().kernel32
        try:
            while not self._stopping.is_set():
                if not kernel32.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                    if ctypes.get_last_error() != _ERROR_NO_DATA:  # NO_DATA: the client left already
                        time.sleep(0.05)  # cancelled or failed: look at the stop flag, do not spin
                    kernel32.DisconnectNamedPipe(handle)
                    continue
                if not self._stopping.is_set():
                    self._session(handle)
                kernel32.DisconnectNamedPipe(handle)
        finally:
            with self._lock:
                self._handle = None
            kernel32.CloseHandle(handle)

    def _session(self, handle) -> None:
        raw, _ = _read_line(handle, trigger.MAX_REQUEST_BYTES, trigger.READ_TIMEOUT_S)
        if not raw:  # connected and silent, or gone: not a request
            return
        try:
            reply = self._handler(raw)
        except Exception:  # noqa: BLE001
            trigger.log.exception("Trigger-Anfrage fehlgeschlagen")
            reply = trigger.format_response("broken")
        if _write(handle, reply):
            with _Deadline(handle, trigger.READ_TIMEOUT_S):  # a client that never reads must not hold the pipe
                _api().kernel32.FlushFileBuffers(handle)


def send_request(name: str, line: bytes, timeout_s: float = 2.0) -> bytes:
    """Send ``line``, return the reply line. FileNotFoundError, PermissionError, TimeoutError or OSError."""
    from ctypes import wintypes

    kernel32 = _api().kernel32
    deadline = time.monotonic() + timeout_s
    while True:
        handle = kernel32.CreateFileW(
            name, _GENERIC_READ_WRITE, 0, None, _OPEN_EXISTING, _SECURITY_SQOS_PRESENT, None
        )
        if handle is not None and handle != _api().invalid:
            break
        error = ctypes.get_last_error()
        if error == _ERROR_FILE_NOT_FOUND:
            raise FileNotFoundError(name)
        if error == _ERROR_ACCESS_DENIED:
            raise PermissionError(name)
        remaining = deadline - time.monotonic()
        if error != _ERROR_PIPE_BUSY or remaining <= 0:
            raise TimeoutError(name) if error == _ERROR_PIPE_BUSY else ctypes.WinError(error)
        if not kernel32.WaitNamedPipeW(name, max(1, int(remaining * 1000))):
            error = ctypes.get_last_error()
            if error == _ERROR_FILE_NOT_FOUND:
                raise FileNotFoundError(name)
            if error != _ERROR_SEM_TIMEOUT:
                raise ctypes.WinError(error)
    try:
        count = wintypes.DWORD()
        if not kernel32.WriteFile(handle, line, len(line), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        raw, timed_out = _read_line(handle, trigger.MAX_REQUEST_BYTES, max(0.05, deadline - time.monotonic()))
        if not raw and timed_out:
            raise TimeoutError(name)
        if not raw:
            raise ConnectionError("pipe closed without a reply")
        return raw
    finally:
        kernel32.CloseHandle(handle)


def _attach_console() -> None:
    """A windowed exe has no stdout; show the result in the console of the program that started it, if any."""
    try:
        if sys.stdout is not None and sys.stderr is not None:
            return
        if _api().kernel32.AttachConsole(_ATTACH_PARENT_PROCESS):
            if sys.stdout is None:
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
            if sys.stderr is None:
                sys.stderr = sys.stdout
    except Exception:  # noqa: BLE001 - no console: the exit code still tells the result
        pass


def _say(text: str) -> None:
    try:
        if sys.stdout is not None:
            print(text, flush=True)
    except Exception:  # noqa: BLE001
        pass


def client_main(id_text: str) -> int:
    """Run the trigger of slot ``id_text`` (``F57``); prints ``ok`` or ``error <code>`` and returns the exit code."""
    _attach_console()
    match = _ID.fullmatch(id_text)
    if match is None or int(match.group(1)) > trigger.MAX_SLOT:
        _say(f"error bad_id (expected F1 to F{trigger.MAX_SLOT})")
        return 2
    line = f"{trigger.PROTOCOL} TRIGGER F{int(match.group(1))}\n".encode("ascii")
    try:
        name = trigger.pipe_name(current_user_sid())
    except (AttributeError, OSError, ValueError):  # AttributeError: no ctypes.WinDLL, not Windows
        _say("error unsupported")
        return trigger.EXIT_CODES["unsupported"]
    try:
        code = trigger.parse_response(send_request(name, line))
    except FileNotFoundError:
        code = "pipe_not_found"
    except PermissionError:
        code = "access_denied"
    except TimeoutError:
        code = "timeout"
    except (OSError, ValueError):
        code = "protocol"
    _say("ok" if code == "ok" else f"error {code}")
    return trigger.EXIT_CODES[code]
