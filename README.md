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

The **Update** page of the program shows the notes of the latest release, downloads its `MourseDecoder.exe`, checks it against the published SHA-256 and replaces itself (the program closes and starts again). The same page removes the program completely: the exe, the settings, your own alphabets, smiley sets and languages (`%APPDATA%\MourseDecoder`, and the old `PolyglotMorse` folder). Nothing else is touched.

To build the executable yourself (after the quick start below), run `pip install pyinstaller` and `python packaging/build.py`. The result is placed in `dist/`.

To publish a new release, push a tag (`git tag v.0.1.2 && git push origin v.0.1.2`). The `release.yml` workflow then builds the EXE and attaches it to the release.

## Quick start (Windows)

Requirements: [Rust](https://rustup.rs) (stable, ≥ 1.83) and Python ≥ 3.10.

```powershell
git clone https://github.com/Interacey/mourse-decoder.git ; cd mourse-decoder
python -m venv .venv ; .venv\Scripts\activate
pip install maturin pytest
maturin develop --release        # builds src/ → python/mourse_decoder/_morse_core.pyd
python -m mourse_decoder         # start the tray app
python -m mourse_decoder --headless --dry-run   # console mode, types nothing
```

The default key is **Scroll Lock**. A short press is a dot, a long press (≥ 200 ms) is a dash, a pause of 600 ms ends a letter, and a pause of 1400 ms inserts a space. All values can be changed in the tray menu under "Settings …".

The Morse key can also be a combination such as Caps Lock + Q: click the key button, hold Caps Lock (or Ctrl, Alt, Shift, Win) and press the key. Q on its own keeps typing normally. While "Block normal input" is on, Caps Lock no longer switches capitals when it is part of a Morse combination.

With "Separate dash key" switched on, the duration no longer matters: the Morse key always gives a dot and a second key always a dash. The pause after the last key still completes the letter. Both keys can be combinations.

### Switching alphabets with a shortcut

In the settings, under "Switch list", pick the alphabets you want to switch through and a shortcut (for example Ctrl+Alt+F9; empty = none). Each press goes to the next alphabet of your list, in the order shown, and a small message names the alphabet and its place (`Deutsch (2/3)`). The message always appears, even with "Show notices" off, because it is the only sign that the shortcut worked. Alphabets you deleted are skipped, and an empty list says so. The shortcut must differ from the on/off shortcut.

On Linux, the packages `libx11-dev libxtst-dev libxi-dev` are also required.

### Emoji and long texts (smiley sets)

The built-in set "Standard" has 125 emoji (faces, gestures, hearts, symbols, food and things such as 😊 😍 🔥 💯 ✅ 🚀 ☕), each on an 8-symbol code that no alphabet uses, so a letter is never shadowed. The easiest codes to key belong to the most common emoji. The Smileys page lists every code. Your own sets work the same way; see below.

A code in a smiley set can output any text, however long, and press key combinations written in braces, as many in a row as you like:

```json
{"id": "macros", "name": "Macros", "codes": {
  "--..--..": "Kind regards\nJane Doe",
  "--..-.-.": "{Ctrl+A}{Ctrl+C}",
  "--...-..": "Hello{Enter}{Ctrl+S}",
  "--..--.-": "{Win+R}{WaitWindow Run}notepad{Enter}{WaitWindow Untitled}Hello"
}}
```

`{WaitWindow Untitled}` waits until the title of the window in front contains "Untitled" (case is ignored). If no such window shows up within 10 seconds, the rest of the macro is dropped instead of typed into the wrong window. `{Wait 500}` simply pauses for 500 ms.

`{Loop 3}…{EndLoop}` repeats everything in between (text, keys, waits) three times. Loops can be nested. At most 1000 repetitions per loop and one million characters in total are allowed. A loop without its `{EndLoop}` stops the macro before anything is typed.

Modifiers: `Ctrl`/`Strg`, `Alt`, `Shift`/`Umschalt`, `Win`/`Windows`. Keys: one character, `F1`–`F12`, `Enter`/`Eingabe`, `Tab`, `Esc`, `Backspace`/`Rücktaste`, `Delete`/`Entf`, `Space`/`Leertaste`, `Home`/`Pos1`, `End`/`Ende`, `PageUp`/`BildAuf`, `PageDown`/`BildAb`, `Up`/`Hoch`, `Down`/`Runter`, `Left`/`Links`, `Right`/`Rechts`. The commands have German names too: `{Warte 500}`, `{WarteFenster …}`, `{Schleife 3}…{SchleifenEnde}`.

Protection against mistakes:
- The whole macro is checked first. A typo such as `{Crtl+C}` stops it with a message, and nothing is typed. Braces without letters or digits, such as `{^_^}`, are typed as text.
- The on/off shortcut stops a running macro at once, even in the middle of a wait or a loop.
- Morse input made while a macro runs is dropped, so it does not come out as stray letters afterwards.
- Saving is silent. Only errors are reported, and switching Morse input off on purpose while a macro runs stops it without a message.

Everything is typed like keyboard input: text, line breaks, tabs and emoji. On Windows only texts of 2000 characters or more (about 40 seconds of typing) are pasted through the clipboard, and everything that was on the clipboard (text, formatted text, images, files) is restored afterwards. Typing uses its own `SendInput` per character with short pauses (20 ms, 150 ms after Return and Tab, 60 ms around the halves of an emoji), because Windows remembers only the last injected character and an app that is a moment late reads two queued characters as "the last one" ("Tab" came out as "aab" in Notepad, and the second emoji in a row broke). Long texts are typed in pieces of 100 characters, so switching Morse input off stops them within about two seconds.

#### Clipboard and files

`{Clipboard}` (`{Zwischenablage}`) types the text that is on the clipboard at that moment. It is always typed key by key like keyboard input, never pasted, however long it is or whatever it contains (line breaks, braces, emoji), so it also works in fields that refuse a paste. That covers a password copied from KeePass: copy it in KeePass first, then key the code (KeePass wipes the clipboard after its timeout, 12 s by default). The text is not shown on the Live page. An empty clipboard stops the macro with a message.

`{File C:\Users\me\report.pdf}` (`{Datei …}`, `{Folder …}`, `{Ordner …}`) puts a file or folder on the clipboard like Explorer's copy and presses Ctrl+V, so it is pasted into the window in front: into a folder it is copied, into a mail or chat it is attached. Several at once: `{File C:\a.txt | C:\Fotos}`. `%USERPROFILE%` and `~` work. A path that does not exist stops the macro before anything is typed.

```json
"--..-.--": "Hello,{Enter}here is the report:{Enter}{File %USERPROFILE%\\Documents\\report.pdf}",
"--..-..-": "{Clipboard}{Enter}"
```

### Chinese telecodes (optional)

```bash
# Unihan.zip from https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip
mourse-decoder-build-telecodes Unihan.zip "%APPDATA%\MourseDecoder\telecode.sqlite"
```

Afterwards, "Chinese (telegraph code)" appears in the alphabet list. The user keys in four digits. The first three appear immediately and are replaced by the character after the fourth (`0022` → 中).

## Configuration files (JSON)

Everything the program reads is JSON. Files live in one folder, `%APPDATA%\MourseDecoder` on Windows (`~/.config/mourse-decoder` on Linux, `~/Library/Application Support/MourseDecoder` on macOS). The pages "Alphabets" and "Smileys" have a Folder button that opens the right sub-folder.

```
MourseDecoder/
  settings.json     the settings page writes it, you may also edit it
  alphabets/        your own alphabets        (<id>.json)
  smileys/          your own smiley sets      (<id>.json)
  languages/        your own languages        (<id>.json)
  telecode.sqlite   optional, see "Chinese telecodes"
```

General rules for all four kinds of file:
- Plain UTF-8 JSON (a BOM is fine). Keys must not repeat inside one object; a duplicate is reported, never silently overwritten.
- The `id` is also the file name: `id` `mein-alphabet` lives in `mein-alphabet.json`. Allowed: `a-z`, `0-9`, `-`, `_`, starting with a letter or digit.
- Files you write by hand may hold raw line breaks inside texts and a stray `"` (`"NIV"`). The app accepts them and saves strict JSON back. A broken file is skipped with a message, it never stops the program.
- Files are size-unlimited on purpose, but a text of several 100 KB takes minutes to type; see "Emoji and long texts".
- Importing: the Import buttons copy any JSON file into the right folder, take a missing `id` from the file name, and check it completely. If the file holds commands that do something when keyed (`{Win+R}`, `{File …}`, `{Clipboard}`) you are asked first.

### settings.json

All keys are optional. A missing key gets its default, an unknown key is ignored. A file with an invalid value is moved to `settings.json.bak` and the defaults are used.

```json
{
  "dot_threshold_ms": 200,
  "letter_gap_ms": 600,
  "word_gap_ms": 1400,
  "trigger": "ScrollLock",
  "separate_dash_key": false,
  "dash_trigger": "Pause",
  "alphabet": "latin",
  "smileys": "standard",
  "enabled": true,
  "language": "de",
  "theme": "system",
  "sidebar_collapsed": false,
  "notifications": true,
  "block_input": false,
  "block_shortcut": "Ctrl+Shift+Pause",
  "cycle_shortcut": "Ctrl+Alt+F9",
  "cycle_alphabets": ["latin", "german", "greek"]
}
```

| Key | Meaning | Allowed |
|---|---|---|
| `dot_threshold_ms` | A press shorter than this is a dot, longer is a dash (ignored with a separate dash key) | 50 to 2000 |
| `letter_gap_ms` | Silence that ends a letter | 100 to 5000 |
| `word_gap_ms` | Silence that adds a space; `0` = never | 0 to 15000, and above `letter_gap_ms` unless 0 |
| `trigger` | The Morse key (the dot key with a separate dash key) | a key binding, see below |
| `separate_dash_key` | On: `trigger` always gives a dot and `dash_trigger` a dash | `true` or `false` |
| `dash_trigger` | The dash key | a key binding, different from `trigger` |
| `alphabet` | Id of the active alphabet, or `chinese-telecode` | an id from `alphabets/` or the built-in ones |
| `smileys` | Id of the smiley set; `""` = none | an id, or empty |
| `enabled` | Morse input on or off | `true` or `false` |
| `language` | Id of the interface language | `en`, `de`, `de-saar`, `es`, `fr`, `it`, `pt` or one of yours |
| `theme` | Appearance | `"system"`, `"light"`, `"dark"` |
| `sidebar_collapsed` | Sidebar shows icons only | `true` or `false` |
| `notifications` | Small message when the on/off shortcut fires | `true` or `false` |
| `block_input` | Hide the trigger's normal function (a space, a click) while Morse input is on | `true` or `false` |
| `block_shortcut` | Shortcut that switches Morse input and blocking on or off; `""` = none | a key binding, see below |
| `cycle_shortcut` | Shortcut that switches to the next alphabet of `cycle_alphabets`; `""` = none; must differ from `block_shortcut` | a key binding |
| `cycle_alphabets` | Your own pick of alphabet ids, switched in this order | a list of up to 64 different ids |

**Key bindings** are one key, or modifiers joined with `+` in front of one key: `F8`, `Ctrl+Shift+Pause`, `CapsLock+VK81`. Modifiers: `Ctrl`, `Alt`, `Shift`, `Win`, `CapsLock` (German names such as `Strg` and `Umschalt` work too). Keys: `ScrollLock`, `Pause`, `Insert`, `ControlRight`, `AltGr`, `F1` to `F12`, `Space`, `MouseLeft`, `MouseRight`, `MouseMiddle`, `Mouse4` to `Mouse32`, and any other key as its Windows virtual-key code `VK1` to `VK255`. The easy way: click a key button on the settings page, press the key. If you press nothing for 8 seconds, a shortcut is removed and a Morse key stays as it was.

### Alphabets (`alphabets/<id>.json`)

```json
{
  "id": "mein-alphabet",
  "name": "Mein Alphabet",
  "description": "Latein plus ein paar Zeichen",
  "extends": "latin",
  "word_separator": " ",
  "codes": {
    ".-.-": "Ä",
    "--..--..": "Hallo{Enter}Welt"
  },
  "combining": {
    "-.-.-.-": "゙"
  }
}
```

| Key | Meaning |
|---|---|
| `id` | Required, must equal the file name without `.json`. Ids of built-in alphabets (`latin`, `german`, `greek`, `cyrillic`, `wabun`, `common`) are taken |
| `name` | Name in the lists; the id if missing |
| `description` | Free text |
| `extends` | Id of an alphabet whose codes you inherit; yours win on the same code. May be a built-in one or another of yours, chains are fine, cycles are refused. `common` brings digits and punctuation |
| `codes` | Required unless inherited: Morse code to output. A code is 1 to 32 characters of `.` and `-`. The output is any non-empty text, with macros (see "Emoji and long texts") |
| `combining` | Codes that change the letter before them instead of typing one, for example Japanese dakuten (`カ` + U+3099 gives `ガ`). The code must be free in the whole chain, so not one that `latin` or `common` already use. The mark is merged with the previous character; an invalid pair types nothing. A code may not be in `codes` and `combining` at once |
| `word_separator` | Typed after a word gap; default is a space, `""` for none (Japanese) |
| `abstract` | `true` = only a base for `extends`, not selectable (like `common`) |

Built-in alphabets cannot be edited; use "Create copy" on the Alphabets page, which gives you a copy to change.

### Smiley sets (`smileys/<id>.json`)

```json
{
  "id": "meine-smileys",
  "name": "Meine Smileys",
  "description": "Emoji und Textbausteine",
  "codes": {
    "--..--..": "🙂",
    "--..-.-.": "Mit freundlichen Grüßen{Enter}Anton",
    "--...-..": "{Ctrl+A}{Ctrl+C}"
  }
}
```

`id`, `codes` (not empty) as for alphabets; `name` and `description` are optional. The active set sits under the alphabet: on the same code the alphabet wins, so a smiley never hides a letter. Use 8-symbol codes, no alphabet has those. The built-in set `standard` is read-only.

### Languages (`languages/<id>.json`)

```json
{
  "id": "nl",
  "name": "Nederlands",
  "strings": {
    "nav.settings": "Instellingen",
    "nav.alphabets": "Alfabetten"
  }
}
```

Copy `python/mourse_decoder/locales/en.json` as a template and translate the values. Keys you leave out fall back to English, so a partial file works. Placeholders such as `{name}` or `{total}` must stay as they are, and only plain names are allowed inside the braces. Pick the language on the settings page, or set `"language": "nl"`.

## Security

- **Network paths are refused** in `{File …}`. Merely checking whether `\\server\share\x` exists makes Windows log in at that server and hand over the user's NTLM hash, so a smiley set from a stranger could leak it. Local drives and mapped drives (`Z:\`) work.
- **Warning before an import:** if a file you import holds commands that do something when keyed (`{Win+R}`, `{Enter}`, `{File …}`, `{Clipboard}`), you are asked once, with the list. Waiting, loops and plain text never ask, and neither do the files that ship with the app. Saving in the editor and typing stay silent.
- **Updates:** only `https://github.com/…` links, a redirect to plain `http` is refused, the answer is capped at 1 MB and the download at 200 MB, the SHA-256 must match, and the file must start like a Windows program (`MZ`). Nothing is installed without a published checksum. The checksum comes from the same release as the exe, so it protects against a damaged download, not against a hijacked release (that would need a signature).
- **Uninstall** deletes only folders named `MourseDecoder`, `mourse-decoder` or `PolyglotMorse`, whatever `APPDATA` says.
- **PowerShell helper:** no `-ExecutionPolicy Bypass`, the script is passed encoded and every path is quoted. Its exit code says what happened (0 done, 1 file stayed busy, 2 replaced but did not start), and the temporary download folder is removed.
- **Language files** from your folder may only use plain `{names}` as placeholders. `{msg.__class__.__init__.__globals__}` and similar are shown as text instead of reading anything.
- **Passwords:** `{Clipboard}` is typed, never logged, never shown on the Live page, and never printed in dry-run mode.

## Memory

Started in the tray, the app uses about 29 MB of private memory (it was 40 MB). Pages are built the first time you open them, and their modules are imported then, so a page you never open costs nothing (the Live page shows what is typed from the moment you open it). The network and process modules of the updater, `sqlite3` and `zipfile` of the telecodes and the Battleship egg load only when they are used. Images for resized windows are capped at about 6 MB instead of piling up.

## Tests

```bash
cargo test                 # 67 Rust tests (timing, worker, trigger, hook routing)
pytest                     # 614 Python tests (some of them against the real Rust module and the real Windows clipboard)
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
| `updater.py`, `ui/update_panel.py` | Latest GitHub release, SHA-256 check, self-replace and complete uninstall | A running exe can neither overwrite nor delete itself, so a hidden PowerShell waits until the app has ended and then does it. Without a published checksum nothing is installed. |
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

