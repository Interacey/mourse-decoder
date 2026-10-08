//! Native text injection.
//!
//! Writes the decoded character into the focused app as if it were typed. On Windows
//! `Enigo::text` sends the characters with `SendInput` and `KEYEVENTF_UNICODE`, so any
//! Unicode character works regardless of the keyboard layout, which a simulation with
//! virtual key codes couldn't do.

use std::cell::RefCell;
use std::fmt;
use std::time::Duration;

use enigo::{Direction, Enigo, Key, Keyboard, Settings};

use crate::hooks;

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

/// Types `text` into the active app.
pub fn inject_text(text: &str) -> Result<(), InjectError> {
    if text.is_empty() {
        return Ok(());
    }
    hooks::suppress_for(SELF_INPUT_GUARD);
    let result = with_enigo(|enigo| enigo.text(text).map_err(|e| InjectError(format!("{e:?}"))));
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
    fn error_message_is_descriptive() {
        assert_eq!(
            InjectError("x".into()).to_string(),
            "Text-Injektion fehlgeschlagen: x"
        );
    }
}
