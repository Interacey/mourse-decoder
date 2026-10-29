# Changelog

## 0.1.1 – 2026-10-29

### New

- **Long texts:** a Morse code can output text of any length. Texts of 200 characters or more are pasted through the clipboard, which is restored afterwards (text, formatted text, images and files).
- **Macros:** output texts can press key combinations such as `{Ctrl+C}` or `{Win+R}`, as many in a row as needed, mixed with text.
- **Macro commands:** `{Wait 500}` pauses, `{WaitWindow Notepad}` waits until a window with that title is in front, and `{Loop 3}…{EndLoop}` repeats (also nested). German names work too: `{Warte 500}`, `{WarteFenster …}`, `{Schleife 3}…{SchleifenEnde}`, `{Eingabe}`, `{Entf}`, `{Hoch}` and more.
- **Separate dash key:** optionally, one key always gives a dot and a second key always a dash, regardless of how long they are pressed.
- **Key combinations as Morse keys:** the Morse key, the dash key and the on/off shortcut can be combinations such as Caps Lock + Q.

### Safer to use

- A macro is checked before it runs. A typo such as `{Crtl+C}` stops it with a message, and nothing is typed.
- The on/off shortcut stops a running macro immediately, even during a wait or a loop.
- Morse input made while a macro runs is dropped instead of coming out as stray letters afterwards.
- `{WaitWindow …}` gives up after 10 seconds instead of typing into the wrong window.
- Saving an alphabet in which an ordinary letter outputs a whole text asks for confirmation first.
- Pasted texts with line breaks or straight quotes are accepted in the alphabet editor and in imported files.

### Fixed

- Line breaks and tabs were typed twice.
- Multi-character output arrived scrambled in busy apps, for example in a freshly opened Windows 11 Notepad tab ("Welt" became "tttt"). Characters are now typed one at a time.
- Errors on the settings page disappeared immediately and were never visible.

## 0.1.0 – 2026-10-08

First release.
