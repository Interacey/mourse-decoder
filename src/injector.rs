//! Native text injection.
//!
//! Writes the decoded character into the focused app as if it were typed. Characters go in
//! as `KEYEVENTF_UNICODE` events, so any Unicode character works regardless of the keyboard
//! layout, which a simulation with virtual key codes couldn't do. On Windows that is our own
//! `SendInput` (see `win`), elsewhere `Enigo::text`.

use std::cell::RefCell;
use std::fmt;
use std::time::Duration;

use enigo::{Direction, Enigo, Key, Keyboard, Settings};

use crate::hooks;
use crate::trigger;

/// How long our own hook stays deaf after an injection (see [`hooks::suppress_for`]).
/// Generous, because synthetic events can reach the hook asynchronously.
const SELF_INPUT_GUARD: Duration = Duration::from_millis(50);

#[derive(Debug)]
pub struct InjectError(pub String);

impl fmt::Display for InjectError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Text-Injektion fehlgeschlagen: {}", self.0)
    }
}

impl std::error::Error for InjectError {}

thread_local! {
    // thread_local instead of a global mutex: Enigo isn't `Send` on every platform
    // (Windows/macOS hold OS handles), and injection nearly always happens on the
    // same worker thread, so the instance is created only once.
    static ENIGO: RefCell<Option<Enigo>> = const { RefCell::new(None) };
}

fn with_enigo<R>(f: impl FnOnce(&mut Enigo) -> Result<R, InjectError>) -> Result<R, InjectError> {
    ENIGO.with(|cell| {
        let mut slot = cell.borrow_mut();
        if slot.is_none() {
            let enigo =
                Enigo::new(&Settings::default()).map_err(|e| InjectError(format!("{e:?}")))?;
            *slot = Some(enigo);
        }
        let result = f(slot.as_mut().expect("gerade initialisiert"));
        if result.is_err() {
            // the connection may be broken (e.g. X server restarted), rebuild it next time
            *slot = None;
        }
        result
    })
}

/// A piece of text to type: plain characters, or a key that stands for a control character.
#[derive(Debug, PartialEq, Eq)]
enum Segment<'a> {
    Text(&'a str),
    Key(Key),
}

/// Splits text at line breaks and tabs, which are typed as keys.
fn segments(text: &str) -> Vec<Segment<'_>> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let key = match c {
            '\r' => {
                if let Some(&(_, '\n')) = chars.peek() {
                    chars.next(); // "\r\n" is one line break
                }
                Key::Return
            }
            '\n' => Key::Return,
            '\t' => Key::Tab,
            _ => continue,
        };
        if start < i {
            out.push(Segment::Text(&text[start..i]));
        }
        out.push(Segment::Key(key));
        start = chars.peek().map_or(text.len(), |&(next, _)| next);
    }
    if start < text.len() {
        out.push(Segment::Text(&text[start..]));
    }
    out
}

/// Pause after every typed character.
///
/// Windows remembers only the LAST injected Unicode character, and the app reads that value
/// when it gets round to a key message. Two characters injected before it did turn into two
/// copies of the second one: a fresh Windows 11 Notepad tab turned "Hallo Welt" into
/// "MorsollaH ttttttttt", "Tab" after a Tab key into "aab", and the second emoji in a row
/// into two low surrogates. Asking the app whether it is idle (WM_NULL) did not help, it
/// answers before it works off its key queue, so only waiting does. 4 ms scrambled plain
/// text in Notepad, 15 ms is the shortest pause that kept it intact.
const CHAR_DELAY: Duration = Duration::from_millis(20);
/// After Return and Tab the app lays out the text and is slow for a moment.
const LINE_KEY_DELAY: Duration = Duration::from_millis(150);

fn pause(delay: Duration) {
    hooks::suppress_for(SELF_INPUT_GUARD); // typing may outlast the guard set at the start
    std::thread::sleep(delay);
}

/// Types one character as if it came from the keyboard.
fn type_char(enigo: &mut Enigo, c: char) -> Result<(), InjectError> {
    #[cfg(windows)]
    {
        let _ = enigo; // our own SendInput, see `win`
        win::type_char(c)
    }
    #[cfg(not(windows))]
    {
        let mut buffer = [0u8; 4];
        enigo
            .text(c.encode_utf8(&mut buffer))
            .map_err(|e| InjectError(format!("{e:?}")))
    }
}

#[cfg(windows)]
mod win {
    //! Typing on Windows: one `SendInput` per UTF-16 unit. enigo sends a whole batch, and its
    //! key-up of a second surrogate carries the first one.

    use std::mem::size_of;
    use std::time::Duration;

    use windows::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYBD_EVENT_FLAGS, KEYEVENTF_KEYUP,
        KEYEVENTF_UNICODE, VIRTUAL_KEY,
    };

    use super::InjectError;

    /// Between the two halves of an emoji and after it: the app is slow with those.
    const SURROGATE_DELAY: Duration = Duration::from_millis(60);

    fn unit_event(unit: u16, up: bool) -> INPUT {
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: VIRTUAL_KEY(0), // must be zero for KEYEVENTF_UNICODE
                    wScan: unit,
                    dwFlags: KEYEVENTF_UNICODE
                        | if up { KEYEVENTF_KEYUP } else { KEYBD_EVENT_FLAGS(0) },
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }

    pub fn type_char(c: char) -> Result<(), InjectError> {
        let mut buffer = [0u16; 2];
        let units = c.encode_utf16(&mut buffer);
        let pair = units.len() == 2;
        // one SendInput per half, both halves in one batch came out as two low surrogates
        for (i, &unit) in units.iter().enumerate() {
            if i > 0 {
                super::pause(SURROGATE_DELAY); // the second half must not overtake the first
            }
            let events = [unit_event(unit, false), unit_event(unit, true)];
            // SAFETY: `events` is a valid slice of initialised INPUT structs
            let sent = unsafe { SendInput(&events, size_of::<INPUT>() as i32) };
            if sent != events.len() as u32 {
                return Err(InjectError(format!("SendInput hat {sent} von 2 Ereignissen gesendet")));
            }
        }
        if pair {
            super::pause(SURROGATE_DELAY);
        }
        Ok(())
    }
}

/// Types `text` into the active app, one character at a time.
pub fn inject_text(text: &str) -> Result<(), InjectError> {
    if text.is_empty() {
        return Ok(());
    }
    hooks::suppress_for(SELF_INPUT_GUARD);
    let result = with_enigo(|enigo| {
        for segment in segments(text) {
            match segment {
                Segment::Text(part) => {
                    for c in part.chars() {
                        type_char(enigo, c)?;
                        pause(CHAR_DELAY);
                    }
                }
                Segment::Key(key) => {
                    enigo
                        .key(key, Direction::Click)
                        .map_err(|e| InjectError(format!("{e:?}")))?;
                    pause(LINE_KEY_DELAY);
                }
            }
        }
        Ok(())
    });
    hooks::suppress_for(SELF_INPUT_GUARD);
    result
}

/// Parses a key combination such as `Ctrl+Shift+T` or `Enter`: modifiers, then one key
/// (a single character or a key name).
pub fn parse_combo(combo: &str) -> Result<(Vec<Key>, Key), String> {
    let parts: Vec<&str> = combo.split('+').map(str::trim).collect();
    let (last, modifiers) = parts.split_last().ok_or("leere Tastenkombination")?;
    let modifiers = modifiers
        .iter()
        .map(|name| match trigger::modifier_group(name) {
            Some(trigger::CTRL) => Ok(Key::Control),
            Some(trigger::ALT) => Ok(Key::Alt),
            Some(trigger::SHIFT) => Ok(Key::Shift),
            Some(trigger::WIN) => Ok(Key::Meta),
            Some(trigger::CAPS_LOCK) => Ok(Key::CapsLock),
            _ => Err(format!("Unbekannte Zusatztaste '{name}'")),
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok((modifiers, key_by_name(last)?))
}

fn key_by_name(name: &str) -> Result<Key, String> {
    let mut chars = name.chars();
    if let (Some(c), None) = (chars.next(), chars.next()) {
        // lowercase, so "Ctrl+C" doesn't press an implicit Shift for the capital letter
        return Ok(Key::Unicode(c.to_lowercase().next().unwrap_or(c)));
    }
    const F_KEYS: [Key; 12] = [
        Key::F1,
        Key::F2,
        Key::F3,
        Key::F4,
        Key::F5,
        Key::F6,
        Key::F7,
        Key::F8,
        Key::F9,
        Key::F10,
        Key::F11,
        Key::F12,
    ];
    let lower = name.to_lowercase(); // not ASCII-only, "RÜCKTASTE" must match too
    if let Some(n) = lower
        .strip_prefix('f')
        .and_then(|n| n.parse::<usize>().ok())
    {
        if (1..=12).contains(&n) {
            return Ok(F_KEYS[n - 1]);
        }
    }
    Ok(match lower.as_str() {
        "enter" | "return" | "eingabe" => Key::Return,
        "tab" => Key::Tab,
        "esc" | "escape" => Key::Escape,
        "backspace" | "rücktaste" => Key::Backspace,
        "delete" | "del" | "entf" => Key::Delete,
        "space" | "leertaste" => Key::Space,
        "home" | "pos1" => Key::Home,
        "end" | "ende" => Key::End,
        "pageup" | "bildauf" => Key::PageUp,
        "pagedown" | "bildab" => Key::PageDown,
        "up" | "hoch" => Key::UpArrow,
        "down" | "runter" => Key::DownArrow,
        "left" | "links" => Key::LeftArrow,
        "right" | "rechts" => Key::RightArrow,
        _ => return Err(format!("Unbekannte Taste '{name}'")),
    })
}

/// Holds `modifiers`, clicks `key`, then lets go of the modifiers again.
pub fn press_keys(modifiers: &[Key], key: Key) -> Result<(), InjectError> {
    hooks::suppress_for(SELF_INPUT_GUARD);
    let result = with_enigo(|enigo| {
        let mut held = Vec::new();
        let mut result = Ok(());
        for &m in modifiers {
            result = enigo.key(m, Direction::Press);
            if result.is_err() {
                break;
            }
            held.push(m);
        }
        if result.is_ok() {
            result = enigo.key(key, Direction::Click);
        }
        // release even after an error: a stuck Ctrl would wreck every following keystroke
        for &m in held.iter().rev() {
            let _ = enigo.key(m, Direction::Release);
        }
        result.map_err(|e| InjectError(format!("{e:?}")))
    });
    hooks::suppress_for(SELF_INPUT_GUARD);
    result
}

/// Deletes `count` characters left of the cursor.
///
/// Needed for composed characters: in Wabun `カ` is typed right away, and when the
/// dakuten follows it has to become `ガ` (one backspace, then "ガ"). Chinese telecodes
/// do the same with four digits that get replaced by the character.
pub fn inject_backspaces(count: u32) -> Result<(), InjectError> {
    if count == 0 {
        return Ok(());
    }
    hooks::suppress_for(SELF_INPUT_GUARD);
    let result = with_enigo(|enigo| {
        for _ in 0..count {
            enigo
                .key(Key::Backspace, Direction::Click)
                .map_err(|e| InjectError(format!("{e:?}")))?;
        }
        Ok(())
    });
    hooks::suppress_for(SELF_INPUT_GUARD);
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    // real injection needs a display and would type into nothing in CI,
    // so only the cases without system access are tested

    #[test]
    fn empty_text_is_a_no_op() {
        assert!(inject_text("").is_ok());
    }

    #[test]
    fn zero_backspaces_is_a_no_op() {
        assert!(inject_backspaces(0).is_ok());
    }

    #[test]
    fn line_breaks_and_tabs_become_single_keys() {
        use Segment::{Key as K, Text as T};
        assert_eq!(
            segments("a\nb\r\nc\td\re"),
            vec![
                T("a"),
                K(Key::Return),
                T("b"),
                K(Key::Return),
                T("c"),
                K(Key::Tab),
                T("d"),
                K(Key::Return),
                T("e")
            ]
        );
        assert_eq!(segments("\n\n"), vec![K(Key::Return), K(Key::Return)]);
        assert_eq!(segments("ü€😀"), vec![T("ü€😀")]);
        assert_eq!(segments(""), vec![]);
    }

    #[test]
    fn combos_parse_modifiers_and_key() {
        assert_eq!(
            parse_combo("Ctrl+Shift+T"),
            Ok((vec![Key::Control, Key::Shift], Key::Unicode('t')))
        );
        assert_eq!(
            parse_combo(" strg + v "),
            Ok((vec![Key::Control], Key::Unicode('v')))
        );
        assert_eq!(parse_combo("Enter"), Ok((vec![], Key::Return)));
        assert_eq!(parse_combo("alt+F4"), Ok((vec![Key::Alt], Key::F4)));
        assert_eq!(parse_combo("Win+Up"), Ok((vec![Key::Meta], Key::UpArrow)));
        assert_eq!(parse_combo("F12"), Ok((vec![], Key::F12)));
        assert_eq!(parse_combo("ä"), Ok((vec![], Key::Unicode('ä'))));
        assert_eq!(
            parse_combo("Umschalt+Eingabe"),
            Ok((vec![Key::Shift], Key::Return))
        );
        assert_eq!(
            parse_combo("Windows+Hoch"),
            Ok((vec![Key::Meta], Key::UpArrow))
        );
        assert_eq!(parse_combo("RÜCKTASTE"), Ok((vec![], Key::Backspace)));
    }

    #[test]
    fn bad_combos_are_rejected() {
        for bad in ["", "Ctrl+", "Hyper+C", "Ctrl+Foo", "F13", "F0", "ab"] {
            assert!(parse_combo(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn error_message_is_descriptive() {
        assert_eq!(
            InjectError("x".into()).to_string(),
            "Text-Injektion fehlgeschlagen: x"
        );
    }
}
