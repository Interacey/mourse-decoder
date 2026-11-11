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

The **Update** page of the program shows the notes of the latest release, downloads its `MourseDecoder.exe`, checks it against the published SHA-256 and replaces itself (the program closes and starts again). If the update cannot run by itself (GitHub's request limit, no connection, no checksum file in the release, an exe folder you cannot write to, PowerShell blocked), the page shows the reason and a button "Open download page" so you can download the exe by hand (nothing opens by itself); a checksum that does not match never opens it. The same page uninstalls the program: after one confirmation it asks whether your own files (settings, cache, JSON files, alphabets, smiley sets and languages in `%APPDATA%\MourseDecoder`, and the old `PolyglotMorse` folder) should be deleted too. Yes deletes the exe and these folders, No deletes only the exe, Cancel or closing the question deletes nothing. Nothing else is touched.

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

### Typing delay

After every character the program types it pauses for a moment (default 20 ms), because some programs read several queued characters as the last one and scramble the text. If a program still drops or mixes up letters, raise "Typing delay" in the settings (0 to 10000 ms); a lower value types faster, but below about 15 ms Windows Notepad garbled text in tests, and 0 means no pause at all. The pause after Enter and Tab is fixed.

Three places can set it, and the most specific wins:

1. `{Delay N}` inside the macro text: from that point on to the end of the output that is being typed (a later `{Delay M}` replaces it). It does not carry over to the next macro run or Morse letter.
2. `typing_delay_ms` of a function key slot in `function_keys.json` (the "Typing delay" field in the slot's editor; empty = not set). It applies to every character that slot types.
3. `typing_delay_ms` in `settings.json` (the "Typing delay" setting). It applies to everything else, including everything typed by Morse letters and by codes of alphabets and smiley sets.

### Undo the last output

The tray menu entry "Undo last output" and the optional shortcut "Shortcut to undo" (settings page, field `undo_shortcut`) delete what was typed last by sending as many Backspaces as it had characters. Only the last output can be undone, and only once: after an undo there is nothing to undo until something new is typed, so repeated presses never delete more text. What counts as the last output: one decoded letter or word gap, or one run of a macro (a function key or a code with a macro text). Counted are the characters actually typed (a line break is one) and the keys `Enter`, `Tab` and `Space` as one each. Any other key in a macro (`{Ctrl+A}`, `{Delete}` …), a pasted file or a correction that deleted earlier text makes the result unknown, so it counts as nothing and nothing before it is deleted. Text that Esc cut short is not counted. Scrolling and waiting change nothing and leave the earlier output undoable. Nothing happens while a macro is typing or Morse input is off; a short message says why. The shortcut waits up to 3 seconds for Ctrl, Alt, Shift and Win to be let go, because Ctrl+Backspace would delete whole words.

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

Inside a loop a counter is available. It starts at 0. `{Loop 5 n}` names it `n` (any name made of letters, digits and `_`, not starting with a digit); without a name it is `i`. `[n]` types its value, `[= …]` types the result of a calculation. Square brackets are used because `{i}` stays the key `i`:

```
{Loop 5 i}[i] {EndLoop}                          0 1 2 3 4
{Loop 5}[= i*2+1] {EndLoop}                      1 3 5 7 9
{Loop 3 j}{Loop 2 i}[= i+j*10] {EndLoop}{EndLoop}  0 1 10 11 20 21
```

German: `{Schleife 3 i}…{SchleifenEnde}`. A calculation knows whole numbers, counters, `+ - * / %`, parentheses and a leading `-` or `+`; there is no `**`. `/` and `%` cut toward zero like in C (`-7/2` is `-3`, `-7%2` is `-1`). No number may exceed 10^15 (neither a result nor one in between), a calculation has at most 200 characters and 32 nested parentheses. Every loop has its own counter; an inner counter with the same name hides the outer one (`{Loop 2 i}{Loop 3 i}[i]{EndLoop}{EndLoop}` types `012012`).

Square brackets stay plain text unless they are a counter: `[name]` counts only if `name` is exactly the counter of a surrounding loop, and `[= …]` only inside a loop and if a letter, digit or parenthesis follows the `=` (after an optional sign). So `[1]`, `[x]`, `[=)]` and everything outside a loop are typed as they are. Inside a loop, a `[= …]` with an unknown counter, a division by 0, a typo or a too long calculation stops the macro before anything is typed. To type a literal `[i]` inside a loop, write `\[i]` (see below).

Counters exist only in the loop body of the one macro that defines them: after `{EndLoop}` the name is gone (`[i]` is text again), a second loop does not see the counter of the first, and one macro never sees another one's counters.

**Literal brackets.** A backslash in front of one of `[ ] ( ) { }` types that character as plain text and disappears. It works everywhere in the macro text, in loops or not, and an escaped character never starts a command or a counter:

| Written | Typed |
| --- | --- |
| `\{Wait 100}` | `{Wait 100}` (no wait) |
| `\[i]` | `[i]` (not the counter, also in a loop with counter `i`) |
| `\[= i+1]` | `[= i+1]` |
| `\(` `\)` `\{` `\}` `\]` | `(` `)` `{` `}` `]` |

Edge cases:

- `\\` directly before one of the six characters types one backslash and the bracket keeps its meaning: `\\[i]` types `\` and the counter. Any other `\\` stays two backslashes.
- A backslash before any other character, or at the end of the text, is typed as it is (`C:\Users`, `\a`). Before a command's `{` it is an escape: to type a backslash right before `{Enter}`, put something between them or write `\\{Enter}`.
- Inside `{File …}`, `{Folder …}` and `{WaitWindow …}` no escape is processed, so Windows paths keep their backslashes. A `\{` in the text in front of such a token still stops it from being a command.
- A command ends at the first `}`. `\}` inside a command (`{Wait 100\}`) does not keep it open; it ends the command, which is then reported as an error.
- Counter values are put in after all escapes were handled and are never searched for escapes or brackets again.

Modifiers: `Ctrl`/`Strg`, `Alt`, `Shift`/`Umschalt`, `Win`/`Windows`. Keys: one character, `F1`–`F12`, `Enter`/`Eingabe`, `Tab`, `Esc`, `Backspace`/`Rücktaste`, `Delete`/`Entf`, `Space`/`Leertaste`, `Home`/`Pos1`, `End`/`Ende`, `PageUp`/`BildAuf`, `PageDown`/`BildAb`, `Up`/`Hoch`, `Down`/`Runter`, `Left`/`Links`, `Right`/`Rechts`. The commands have German names too: `{Warte 500}`, `{WarteFenster …}`, `{Schleife 3}…{SchleifenEnde}`, `{Scroll Runter}`, `{Scroll Hoch}`, `{Scroll Stopp}`.

`{Backspace 5}` presses a key several times (count 1 to 1000 after a space, e.g. `{Space 18}`, `{Ctrl+End 2}`).

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

#### Pausing and rolling back

`{Wait 500}` (`{Warte 500}`) pauses the macro for 500 ms before the next step.

`{Delay 100}` (`{Verzögerung 100}`) sets the pause after every typed character to 100 ms from that point on, for the rest of the output being typed; `a{Delay 100}bc{Delay 5}de` types `a` at the usual pace, `b` and `c` 100 ms apart and `d`, `e` 5 ms apart. It overrides `typing_delay_ms` of the slot and of the settings, see "Typing delay". The value is a whole number from 0 to 10000 ms (the same range as `typing_delay_ms`; 0 = no pause); anything else (negative, text, above 10000) refuses the whole macro with a message and nothing is typed. `{Delay 100}` and `{Delay100}` are the same, case is ignored. It is not a pause by itself and does not count towards the limit for `{Wait}`, and it ends with the macro run. Does not work for single characters from `{Clipboard}` text.

`{Rollback}` (`{Rückgängig}`) deletes typed text with Backspaces. If the macro has already typed something before it (text, `{Clipboard}`, Enter/Tab/Space), it deletes exactly that; otherwise it deletes the last output of the previous macro or Morse letter, the same length that "Undo last output" uses. So a code with `{Clipboard}` followed later by another code with just `{Rollback}` removes exactly what the clipboard code typed, whether that was `abc` or a whole book. One run deletes once: afterwards there is nothing left to roll back until something new is typed. The length counts only what was really typed; after any other key (`{Ctrl+A}`, `{Delete}` …), a pasted file or a correction the length is unknown and `{Rollback}` deletes nothing. The Backspaces are sent without the typing delay, in pieces of 2000 characters; between the pieces Esc and the on/off switch are checked, so even a very long rollback can be stopped, and what was not yet deleted stays available for the next rollback or undo. `{Rollback}` is listed in the warning when you import a file that contains it.

```json
"--..-.--": "{Clipboard}",
"--..-..-": "{Rollback}"
```

#### Scrolling

`{Scroll Down}`, `{Scroll Up}` and `{Scroll Stop}` turn the mouse wheel. Spaces are optional and case is ignored (`{ScrollDown}`). German: `{Scroll Runter}`, `{Scroll Hoch}`, `{Scroll Stopp}`; `{Scroll End}` and `{Scroll Ende}` mean the same as Stop. There are no default Morse codes for them. Bind them like any macro, in your own alphabet or smiley set or on a function-key slot (Stream Deck):

```json
{"id": "scroll", "name": "Scroll", "codes": {
  "--.-.--.": "{Scroll Down}",
  "--.-.-.-": "{Scroll Up}",
  "--.--.-.": "{Scroll Stop}"
}}
```

Keying `{Scroll Down}` starts turning the wheel down at once and keeps going. Each further `{Scroll Down}` makes it faster: 3, 5, 8, 12, 18, 25 notches per second; more triggers stay at 25. One notch is one wheel click, 3 lines with the Windows default. `{Scroll Up}` while scrolling down reverses at the slowest speed, and the other way round.

Scrolling ends on `{Scroll Stop}`, on Esc, when Morse input is switched off with the on/off shortcut, when the program ends, on an injection error (with a message), and by itself after 10 minutes (with a message). Morse letters keep working while it scrolls, which is how the speed-up and stop codes get through; other letters are typed normally meanwhile.

The wheel turns the window under the mouse pointer (Windows 10/11 default), which is not always the window with the keyboard focus. In the Chinese telecode mode only a function key can scroll, because the telecode decoder reads digits only. `{Scroll Down 3}` is an error; to start at speed 3 write `{Loop 3}{Scroll Down}{EndLoop}`. No space follows a scroll code (the word gap after a macro is dropped, as for every macro longer than one character).

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
  function_keys.json  function keys F1..F100, see "Function keys"
  alphabets/        your own alphabets        (<id>.json)
  smileys/          your own smiley sets      (<id>.json)
  languages/        your own languages        (<id>.json)
  telecode.sqlite   optional, see "Chinese telecodes"
```

General rules for all kinds of file:
- Plain UTF-8 JSON (a BOM is fine). Keys must not repeat inside one object; a duplicate is reported, never silently overwritten.
- The `id` is also the file name: `id` `mein-alphabet` lives in `mein-alphabet.json`. Allowed: `a-z`, `0-9`, `-`, `_`, starting with a letter or digit.
- Files you write by hand may hold raw line breaks inside texts and a stray `"` (`"NIV"`). The app accepts them and saves strict JSON back. A broken file is skipped with a message, it never stops the program.
- Files are size-unlimited on purpose, but a text of several 100 KB takes minutes to type; see "Emoji and long texts".
- Importing: the Import buttons copy any JSON file into the right folder, take a missing `id` from the file name, and check it completely. If the file holds commands that do something when keyed (`{Win+R}`, `{File …}`, `{Clipboard}`, `{Scroll …}`) you are asked first.

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
  "cycle_alphabets": ["latin", "german", "greek"],
  "typing_delay_ms": 20,
  "undo_shortcut": "Ctrl+Alt+F8"
}
```

| Key | Meaning | Allowed |
|---|---|---|
| `dot_threshold_ms` | A press shorter than this is a dot, longer is a dash (ignored with a separate dash key) | 50 to 2000 |
| `letter_gap_ms` | Silence that ends a letter | 100 to 5000 |
| `word_gap_ms` | Silence that adds a space; `0` = never | 0 to 15000, and above `letter_gap_ms` unless 0 |
| `typing_delay_ms` | Pause after each typed character; a slot's `typing_delay_ms` and a `{Delay N}` in a macro override it | 0 to 10000, default 20 |
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
| `undo_shortcut` | Shortcut that deletes the last typed output; `""` = none; must differ from `block_shortcut` and `cycle_shortcut` | a key binding |

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

### Function keys (`function_keys.json`)

100 slots, F1 to F100. Each slot has a trigger (a key or combination such as `Ctrl+Shift+F13`), a macro (the same macro language as the codes of an alphabet: commands in braces, loops with counters, backslash escapes), the switches `enabled` and `swallow`, and a free `label`. Pressing the trigger types the macro.

```json
{
  "schema_version": 1,
  "slots": [
    { "slot": 5, "trigger": "Ctrl+F5", "macro": "Mit freundlichen Grüßen{Enter}Anton",
      "enabled": true, "swallow": true, "label": "Gruß", "typing_delay_ms": 40 }
  ]
}
```

- `typing_delay_ms` is optional (0 to 10000): the pause after each character this slot types. It overrides the global `typing_delay_ms` from `settings.json`, and a `{Delay N}` in the macro overrides it from that point on. A slot without it uses the global value, and files from before this field load unchanged.
- Only slots that differ from their default are written. Unknown fields are kept when the file is saved again. A file that cannot be read is moved to `function_keys.json.bak` and the defaults apply; a broken entry is reported, and a macro that does not parse is kept but switched off. A file with a newer `schema_version` is read but never overwritten.
- F1 to F12 have no trigger until you set one. F13 to F24 exist as keys on some keyboards and in Stream Deck software, and default to the bare key. Windows has no F25 to F100, so those slots are virtual: their default triggers are long combinations of Ctrl, Alt, Shift and Win with F13 to F24 that nobody presses by accident, first all four modifiers (`Ctrl+Alt+Shift+Win+F13` is F25), then every three, then pairs. Set a Stream Deck button to send one of them. Every trigger can be changed.
- Modifiers must match exactly: `F13` does not fire while Ctrl is held, `Ctrl+F13` does not fire under Ctrl+Alt. A trigger must differ from the Morse key, the dash key and the three shortcuts (on/off, switch, undo), and from every other active slot. Esc, the modifier keys and plain typing keys (letters, digits, Space, Enter, Backspace, Tab, punctuation, numpad without Ctrl, Alt or Win) are refused as triggers, because the hook would otherwise fire on normal typing; a right or middle click is refused only while it is swallowed and has no modifier.
- `swallow` (default on) hides the trigger key from the focused app. A key without a slot is never touched. On Linux the hook can only watch, so triggers always reach the app.
- Before the macro types, the program waits up to 3 seconds for Ctrl, Alt, Shift and Win to be let go; otherwise typed text would turn into shortcuts. One macro runs at a time, a second trigger meanwhile is ignored. Keys sent by the program itself never fire a slot, so a macro containing `{F13}` does not start itself.
- Pressing Esc stops any running macro, also those of Morse letters, and ends scrolling. Esc itself still reaches the app.

**The Function keys page** lists all 100 slots with type (real or virtual), trigger, label or macro and status; the switch "Only assigned" and the search box narrow the list. Select a slot to edit its macro, label, trigger (click the trigger button, press the keys; "Default" restores the standard one), "Active" and "Swallow the key". The macro is checked while you type and a mistake is shown in red; "Save" is available once the slot is valid and installs it right away. "Test" runs the saved macro after 3 seconds, so you can click into the target window first, and "Reset" returns the slot to its default trigger with an empty macro. For a virtual slot, set the Stream Deck button to send the combination shown in the Trigger column. Esc stops a running macro.

## Backup and restore

The Settings page has "Export backup" and "Import backup" at the bottom. Export saves your configuration as one zip file (default name `mourse-backup-YYYYMMDD.zip`).

**Included:** `settings.json`, `function_keys.json` and every `.json` file in `alphabets/`, `smileys/` and `languages/`, byte for byte as they are on disk, plus a `manifest.json` (program version, creation time in UTC, and for each file its size and SHA-256).

**Not included:** `.bak` copies and temporary files, and `telecode.sqlite` unless you answer Yes when Export asks (it only asks if the file exists). The program cannot rebuild that database by itself: it needs the Unihan download and `mourse-decoder-build-telecodes`, see "Chinese telecodes". It holds no personal data.

**The zip is sensitive.** Function-key macros can contain paths, texts and anything else you typed into them. Treat the zip like a password file and do not share it.

**Never import a zip from a stranger without reading what it holds.** `function_keys.json` can bring macros that start as soon as their key is pressed: `{Win+R}` with `{WaitWindow Run}` and a command line, `{File …}` or `{Folder …}` that open things, `{Clipboard}` that types what you copied. Import therefore shows every enabled macro (slot, trigger, the first 200 characters, control and direction-changing characters written out as `\u202e`) and marks the risky commands. As soon as such macros would be installed, the confirmation has three choices: Yes imports everything, No imports everything except `function_keys.json` (your own function keys stay), Cancel imports nothing. Imported slots are never switched off silently: what you confirm is what is installed.

**Import** checks the whole zip before it writes anything, then shows how many files are new, replaced or unchanged and asks for confirmation. Files that are in the zip replace the files with the same name; all other files in your folder stay. The checking and the writing run in the background (the buttons are off meanwhile) and use the very zip bytes that were checked: if the file changes while the confirmation is open, the import is refused. Before the first write, your current files are saved as a restore point, `MourseDecoder-backup-<date>-<time>.zip` next to the configuration folder (same format: to undo an import, import that zip). The newest 5 restore points are kept; older ones that the program itself made (recognised by name and by a marker in their manifest) are deleted, nothing else is. On Linux and macOS a restore point and an export are readable by you only (mode 600); on Windows they inherit the access rights of the folder they are in, which in your profile is you only. If a write fails, the files already written are put back; if that fails for some of them, the message names them and the restore point.

After an import the program uses the new files right away: the settings and function keys are loaded through the same paths as a change in the program, the pages are refreshed, and the active alphabet and smiley set are read again, so a following change in the tray menu or a page saves the imported state and not the old one. Only language files (their texts) and `telecode.sqlite` are read at the next start; the message says so. If the running program cannot take over the files, the message says that too and asks for a restart. From the moment the import starts writing until the program has taken over the new files, nothing else saves: a change in the tray menu or on a page is refused with the message "An import is running" (the function key editor keeps your edit, save again afterwards), and the shortcuts for switching Morse input and alphabets wait: their key presses are handled right after the import. The error messages of an import are shown in the program's language.

An import is refused, and nothing is changed, if:
- `manifest.json` is missing, or its format version is unknown;
- a name is not one of the files above (paths with `..`, absolute paths, drive letters, backslashes in the name stored in the zip on every system, sub-folders, links, `.bak` files), or a file is listed twice;
- a file name is a Windows device name (`CON`, `NUL`, `COM1` to `COM9` and `LPT1` to `LPT9`, also as `COM¹`, full-width `ＣＯＮ`, `CONIN$`, `CONOUT$`, `CLOCK$`), looks like a short name (`ALPHAB~1.json`), or contains control, direction-changing or zero-width characters;
- a size or SHA-256 does not match the manifest, or the zip holds a file the manifest does not list;
- a JSON file does not parse, or `function_keys.json` has a `schema_version` newer than this program knows;
- `settings.json` is something the program would throw away on start (it would move it to `settings.json.bak` and use the defaults, so the import would be void): it is checked by the same code that loads it at start, and the message names the field, for example the unknown key `bogus-key`. `function_keys.json` is checked by the same loader, macros included; a file it cannot read is refused (with the choice to import the rest without it), single slots it would switch off are listed as notes;
- the function keys and the settings of the bundle (or of the bundle and the files already there) would clash, for example a slot on the Morse key;
- `telecode.sqlite` is no SQLite database, fails `PRAGMA quick_check` or has no table `telecode` with the columns `code` and `char` (checked on a copy in the temp folder, opened read-only; the file in your folder is never opened);
- a file is larger than its limit: `settings.json` 1 MiB, `function_keys.json` 8 MiB, every file in `alphabets/`, `smileys/` and `languages/` 2 MiB, `telecode.sqlite` 50 MiB (real files are far below: settings about 0.5 KB, shipped alphabets under 4 KB, language files about 20 KB; 100 macros of 10 000 characters make about 1 MB, up to 6 MB if every character is written as `\uXXXX`; pasted texts of several 100 KB are the biggest thing users make). Export refuses such a file as well, because a backup that cannot be imported again is no backup;
- the zip has more than 2000 entries, more than 200 MiB in total, or an entry that is compressed more than 100 times over 1 MiB (zip bomb), or an entry cannot be unpacked (damaged data, unsupported compression, password).

## Security

- **Network paths are refused** in `{File …}`. Merely checking whether `\\server\share\x` exists makes Windows log in at that server and hand over the user's NTLM hash, so a smiley set from a stranger could leak it. Local drives and mapped drives (`Z:\`) work.
- **Warning before an import:** if a file you import holds commands that do something when keyed (`{Win+R}`, `{Enter}`, `{File …}`, `{Clipboard}`, `{Scroll …}`), you are asked once, with the list. Waiting, loops and plain text never ask, and neither do the files that ship with the app. Saving in the editor and typing stay silent.
- **Backup zips from others:** a backup can carry function key macros that run programs; the import lists them with the risky commands marked and lets you leave `function_keys.json` out, see "Backup and restore". Only import zips you made or trust completely.
- **Updates:** only `https://github.com/…` links; every hop of a redirect must be `https` on `github.com`, `api.github.com` or `*.githubusercontent.com`, anything else is refused; the answer is capped at 1 MB and the download at 200 MB, the SHA-256 must match, and the file must start like a Windows program (`MZ`). Nothing is installed without a published checksum. The checksum comes from the same release as the exe, so it protects against a damaged download, not against a hijacked release (that would need a signature).
- **Uninstall** (the data part, if you choose it) deletes only folders named `MourseDecoder`, `mourse-decoder` or `PolyglotMorse`, whatever `APPDATA` says, and skips such a folder if it is a junction or symlink.
- **PowerShell helper:** no `-ExecutionPolicy Bypass`, the script is passed encoded and every path is quoted (also against the typographic quotes `‘ ’ ‚ ‛`, which PowerShell treats like `'`) and used literally (`[ ]` are no wildcards). Its exit code says what happened (0 done, 1 file stayed busy, 2 replaced but did not start). If the exe stays locked for about a minute, the old program is started again, the temporary download folder is removed and `update-result.txt` in the settings folder gets a one-word reason, which the next start shows once and deletes.
- **Language files** from your folder may only use plain `{names}` as placeholders. `{msg.__class__.__init__.__globals__}` and similar are shown as text instead of reading anything.
- **Passwords:** `{Clipboard}` is typed, never logged, never shown on the Live page, and never printed in dry-run mode.

## Memory

Started in the tray, the app uses about 29 MB of private memory (it was 40 MB). Pages are built the first time you open them, and their modules are imported then, so a page you never open costs nothing (the Live page shows what is typed from the moment you open it). The network and process modules of the updater, `sqlite3` and `zipfile` of the telecodes and the Battleship egg load only when they are used. Images for resized windows are capped at about 6 MB instead of piling up.

## Tests

```bash
cargo test                 # 108 Rust tests (timing, worker, trigger, hook routing)
pytest                     # 1119 Python tests (1117 passed, 2 skipped on Windows; some of them against the real Rust module and the real Windows clipboard)
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
| `function_keys.rs` | Slot table with a queue of fired slots, Esc detector | Both are state machines that take event, modifiers and time as arguments. The hook callback only writes the slot number into a queue of 64; Python runs the macro. |
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

