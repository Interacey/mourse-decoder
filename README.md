# Mourse Decoder

Mourse Decoder is a system-wide Morse code decoder for Windows and Linux. A key or mouse button serves as the Morse key, and the decoded character is typed directly into the active application. Supported scripts are Latin, German, Greek, Cyrillic, Japanese (Wabun) and Chinese telegraph code.

The program has a hybrid architecture: **Rust** handles everything time-critical (global hooks, timing, text injection), and **Python** handles alphabets, settings and the UI. The two parts are connected through **PyO3/maturin**.

```
┌──────────────────────── PYTHON (python/mourse_decoder) ────────────────────────┐
│  ui/tray.py        Tray icon (pystray) + settings (tkinter)                    │
│  controller.py     Engine events → decoder → injector                          │
│  decoders.py       Sequence → text (dakuten, telecode buffer)                  │
│  dictionary.py     JSON alphabets, lazy loading     telecode.py  SQLite        │
└──────────────────────────────────┬─────────────────────────────────────────────┘
                                   │ PyO3: MorseEngine, inject_text, …
┌──────────────────────────────────▼──────────────── RUST (src/) ────────────────┐
│  hooks.rs    global hook (rdev → Win32 WH_KEYBOARD_LL/WH_MOUSE_LL)             │
│      │  press/release + Instant      (mpsc channel, non-blocking)              │
│  engine.rs   worker thread, sleeps exactly until the next pause deadline       │
│  timing.rs   pure state machine: duration → "." / "-", pause → letter/word     │
│  injector.rs types Unicode text (enigo → SendInput/KEYEVENTF_UNICODE)          │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Download (Windows)

`MourseDecoder.exe` is attached to each release on GitHub. Download it and double-click to run; neither Python nor Rust is required. The file `MourseDecoder.exe.sha256` next to it allows verifying the download. Windows SmartScreen warns about unknown, unsigned programs; choose "More info" → "Run anyway".

To build the executable yourself (after the quick start below), run `pip install pyinstaller` and `python packaging/build.py`. The result is placed in `dist/`.

To publish a new release, push a tag (`git tag v0.1.0 && git push origin v0.1.0`). The `release.yml` workflow then builds the EXE and attaches it to the release.

## Quick start (Windows)

Requirements: [Rust](https://rustup.rs) (stable, ≥ 1.83) and Python ≥ 3.10.

```powershell
git clone <repo> ; cd mourse-decoder-decoder
python -m venv .venv ; .venv\Scripts\activate
pip install maturin pytest
maturin develop --release        # builds src/ → python/mourse_decoder/_morse_core.pyd
python -m mourse_decoder         # start the tray app
python -m mourse_decoder --headless --dry-run   # console mode, types nothing
```

The default key is **Scroll Lock**. A short press is a dot, a long press (≥ 200 ms) is a dash, a pause of 600 ms ends a letter, and a pause of 1400 ms inserts a space. All values can be changed in the tray menu under "Settings …".

On Linux, the packages `libx11-dev libxtst-dev libxi-dev` are also required.

### Chinese telecodes (optional)

```bash
# Unihan.zip from https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip
mourse-decoder-build-telecodes Unihan.zip "%APPDATA%\MourseDecoder\telecode.sqlite"
```

Afterwards, "Chinese (telegraph code)" appears in the alphabet list. The user keys in four digits. The first three appear immediately and are replaced by the character after the fourth (`0022` → 中).

## Tests

```bash
cargo test                 # 42 Rust tests (timing, worker, trigger, hook routing)
pytest                     # 169 Python tests (18 of them against the real Rust module)
ruff check python tests
```

An end-to-end run was also done on Linux with a virtual X display (Xvfb) and `xdotool`. Simulated F8 key presses were captured by the global hook and decoded as "SOS ", and a Greek "Δ" arrived in the focused window through injection. Windows itself could not be run in the development environment; the GitHub Actions matrix (`.github/workflows/ci.yml`, Windows and Linux) covers it.

## Components and design rationale

### Rust (`src/`)

| File | Purpose | Rationale |
|---|---|---|
| `timing.rs` | State machine `MorseTimer`: `on_press(t)`, `on_release(t)`, `poll(now)`, `next_deadline()` | It never reads the clock itself. All timestamps come from outside, so tests are exact ("199 ms = dot, 200 ms = dash") and need no `sleep`. It handles key repeat (repeated key-down events while a key is held), release without a press, and overdue pauses. |
| `engine.rs` | Worker thread with an `mpsc` channel and `recv_timeout(deadline)` | With no open sequence the thread blocks completely, giving **0 % CPU when idle** (measured: 0 CPU ticks in 10 s). If an event is already queued when a deadline has passed, the event is processed first. Otherwise letters would be split incorrectly when a Python callback hangs (a test uncovered exactly this error). |
| `hooks.rs` | One global hook thread per process, routed to the current worker | `rdev::listen` blocks forever and cannot be cancelled, so start and stop only change the route. The callback performs only a lock, a comparison and a `send`, because Windows silently removes low-level hooks that respond too slowly. Timestamps use `Instant` (monotonic) rather than `SystemTime` (which jumps on NTP adjustments). |
| `injector.rs` | `inject_text`, `inject_backspaces` via enigo | `KEYEVENTF_UNICODE` types every character independently of the keyboard layout. Backspaces are needed for dakuten (カ→ガ) and telecodes. A short hook lock prevents feedback, for example when the space bar is the key and a space is injected. |
| `trigger.rs` | Permitted keys as a separate enum | Configuration and tests do not depend on rdev. |
| `python.rs` | PyO3 class `MorseEngine` and functions | `stop()` releases the GIL before the `join` (`py.detach`). Otherwise a deadlock occurs when the worker is waiting for the GIL. Exceptions in callbacks go to `sys.unraisablehook` instead of killing the worker. |

Cargo features: `native-io` (hooks and injection, enabled by default) and `python` (PyO3, set by maturin). This allows `cargo test` to run without Python.

### Python (`python/mourse_decoder/`)

| File | Purpose | Rationale |
|---|---|---|
| `alphabets/*.json` | Alphabets with `extends`, `combining`, `word_separator` | The abstract `common.json` holds digits and punctuation once; child alphabets override entries (Wabun: `.-.-.-` = "、" instead of "."). A new alphabet is a new JSON file and needs no code. |
| `dictionary.py` | `load_alphabet`, `DictionaryManager` | Lazy loading: the file is read only on `select()`, and the previous alphabet is released. Validation is strict. It covers **duplicate JSON keys** (which `json` would otherwise overwrite silently), cycles in `extends`, and path traversal in IDs. |
| `decoders.py` | `Emission(text, erase)`, `AlphabetDecoder`, `TelecodeDecoder` | Scripts with state (dakuten, four-digit codes) are handled without special cases in the controller. Unicode NFC combines カ + U+3099 into ガ. |
| `telecode.py` | SQLite store and build from Unihan | The roughly 10,000 entries stay on disk. `WITHOUT ROWID` with a primary key gives a single B-tree lookup. The database is opened read-only, and the build is atomic through a temporary file. |
| `settings.py` | `Settings` (frozen dataclass), `SettingsStore` | Immutability makes sharing between threads safe. Saving is atomic; a corrupt file is moved to `.bak` and defaults are used. The word pause of 1400 ms follows the ITU ratio of 7:3 to the letter pause. |
| `controller.py` | Wiring of engine → decoder → injector | Dependencies are passed in (protocols), so the controller can be tested entirely with fakes. Errors in the injector or UI are reported and never raised into the Rust thread. |
| `ui/form.py` | Form parsing and validation | GUI-free logic, and therefore testable with pytest. |
| `ui/tray.py`, `ui/main_window.py` (plus `settings_panel`, `alphabet_panel`, `live_panel`) | pystray and tkinter | tkinter was chosen over PySide6 because Qt alone would add more than 50 MB. tkinter is not thread-safe, so all UI tasks from the tray and worker threads pass through a queue to the main thread. |

## Measurements (Linux, Python 3.13, headless mode)

| | Value |
|---|---|
| CPU when idle | 0 ticks in 10 s |
| Memory (RSS) | about 20 MB in total, of which about 9 MB is the bare Python interpreter |
| Native module | about 1.4 MB |

The original goal of under 10 MB of RAM cannot be reached with a Python front end, because the interpreter alone uses nearly that much. The Rust engine is the smallest part of the total.

## Description
Mourse Decoder is a system-wide Morse code decoder for Windows and Linux. A key or mouse button serves as the Morse key, and the decoded characters are typed directly into the active application. It supports Latin, German, Greek, Cyrillic, Japanese (Wabun) and Chinese telegraph code. A Rust core handles the global hooks, timing and text injection, and a Python front end handles alphabets, settings and the tray UI.

P.S. The key and the timing (dot/dash threshold, letter pause, word pause) are configurable, so you can also use Mourse Decoder as a personal input shortcut. Define your own Morse rhythm and type with it in any application.

