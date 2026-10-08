//! Global low-level hooks.
//!
//! One process-wide hook thread watches all keyboard and mouse events and forwards
//! only press/release of the chosen trigger to the running worker as a
//! [`WorkerMessage`].
//!
//! It is global because `rdev::listen` blocks forever and can't be cancelled. On Windows
//! it installs `SetWindowsHookEx(WH_KEYBOARD_LL / WH_MOUSE_LL)` with a message loop, and
//! installing it twice per process is pointless (and sometimes impossible on
//! macOS/Linux). So the thread starts once and is wired to the current worker through a
//! route ([`install_route`] / [`clear_route`]); starting or stopping the engine only
//! changes the route.
//!
//! The callback runs for EVERY mouse move on the system, so it only takes a lock,
//! compares and does a non-blocking `send`. Windows silently removes a hook that is
//! too slow.

use std::io;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use rdev::{Button, EventType, Key};

use crate::engine::WorkerMessage;
use crate::lock_or_recover;
use crate::trigger::TriggerKey;

/// Edge of a trigger.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Edge {
    Press,
    Release,
}

/// Where trigger events go.
struct Route {
    trigger: TriggerKey,
    tx: Sender<WorkerMessage>,
}

static ROUTE: Mutex<Option<Route>> = Mutex::new(None);
static HOOK_STARTED: AtomicBool = AtomicBool::new(false);
static HOOK_ERROR: Mutex<Option<String>> = Mutex::new(None);
/// Trigger events are ignored until this moment (see [`suppress_for`]).
static SUPPRESS_UNTIL: Mutex<Option<Instant>> = Mutex::new(None);

/// While true, the trigger key/click does not reach other apps (only decoded text does).
static BLOCK: AtomicBool = AtomicBool::new(false);
/// Morse input is on. The shortcut toggles this; while it is off nothing is swallowed.
static ACTIVE: AtomicBool = AtomicBool::new(true);
/// The trigger press was swallowed, so its release is swallowed too (no stuck key).
static PRESS_BLOCKED: AtomicBool = AtomicBool::new(false);
/// The shortcut key is held, so key repeat doesn't toggle blocking again and again.
static SHORTCUT_DOWN: AtomicBool = AtomicBool::new(false);
/// Modifier keys currently held, one bit per physical key (see `modifier_bit`).
static MODIFIERS: AtomicU8 = AtomicU8::new(0);
static SHORTCUT: Mutex<Option<Shortcut>> = Mutex::new(None);

const CTRL: u8 = 0b0000_0011;
const ALT: u8 = 0b0000_1100;
const SHIFT: u8 = 0b0011_0000;
const META: u8 = 0b1100_0000;

fn modifier_bit(key: Key) -> Option<u8> {
    match key {
        Key::ControlLeft => Some(1),
        Key::ControlRight => Some(2),
        Key::Alt => Some(4),
        Key::AltGr => Some(8),
        Key::ShiftLeft => Some(16),
        Key::ShiftRight => Some(32),
        Key::MetaLeft => Some(64),
        Key::Unknown(92) => Some(128), // right Windows key
        _ => None,
    }
}

/// A key combination that turns Morse input on or off, e.g. `Ctrl+Shift+Pause`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Shortcut {
    ctrl: bool,
    alt: bool,
    shift: bool,
    meta: bool,
    key: TriggerKey,
}

impl Shortcut {
    /// True if every required modifier is held (extra modifiers don't matter).
    fn modifiers_held(&self, held: u8) -> bool {
        (!self.ctrl || held & CTRL != 0)
            && (!self.alt || held & ALT != 0)
            && (!self.shift || held & SHIFT != 0)
            && (!self.meta || held & META != 0)
    }
}

/// Parses `Ctrl+Alt+Shift+Win+<trigger>`; the last part is any trigger name.
pub fn parse_shortcut(text: &str) -> Result<Shortcut, String> {
    let parts: Vec<&str> = text.split('+').map(str::trim).collect();
    let (key_part, modifiers) = parts.split_last().ok_or("empty shortcut")?;
    let mut shortcut = Shortcut {
        ctrl: false,
        alt: false,
        shift: false,
        meta: false,
        key: key_part.parse::<TriggerKey>().map_err(|e| e.to_string())?,
    };
    for modifier in modifiers {
        match modifier.to_ascii_lowercase().as_str() {
            "ctrl" | "control" | "strg" => shortcut.ctrl = true,
            "alt" => shortcut.alt = true,
            "shift" => shortcut.shift = true,
            "win" | "meta" | "super" => shortcut.meta = true,
            other => return Err(format!("Unknown modifier '{other}'")),
        }
    }
    Ok(shortcut)
}

/// Map an rdev event to the chosen trigger. A pure function, so it's testable without a hook.
pub fn route_event(trigger: TriggerKey, event: &EventType) -> Option<Edge> {
    match *event {
        EventType::KeyPress(key) if key_matches(trigger, key) => Some(Edge::Press),
        EventType::KeyRelease(key) if key_matches(trigger, key) => Some(Edge::Release),
        EventType::ButtonPress(button) if button_matches(trigger, button) => Some(Edge::Press),
        EventType::ButtonRelease(button) if button_matches(trigger, button) => Some(Edge::Release),
        _ => None,
    }
}

fn key_matches(trigger: TriggerKey, key: Key) -> bool {
    let expected = match trigger {
        TriggerKey::ScrollLock => Key::ScrollLock,
        TriggerKey::Pause => Key::Pause,
        TriggerKey::Insert => Key::Insert,
        TriggerKey::ControlRight => Key::ControlRight,
        TriggerKey::AltGr => Key::AltGr,
        TriggerKey::F1 => Key::F1,
        TriggerKey::F2 => Key::F2,
        TriggerKey::F3 => Key::F3,
        TriggerKey::F4 => Key::F4,
        TriggerKey::F5 => Key::F5,
        TriggerKey::F6 => Key::F6,
        TriggerKey::F7 => Key::F7,
        TriggerKey::F8 => Key::F8,
        TriggerKey::F9 => Key::F9,
        TriggerKey::F10 => Key::F10,
        TriggerKey::F11 => Key::F11,
        TriggerKey::F12 => Key::F12,
        TriggerKey::Space => Key::Space,
        // mouse triggers never match a keyboard key
        // any key, by virtual-key code
        TriggerKey::Vk(code) => return vk_of(key) == Some(code),
        TriggerKey::MouseLeft
        | TriggerKey::MouseRight
        | TriggerKey::MouseMiddle
        | TriggerKey::MouseButton(_) => return false,
    };
    key == expected
}

fn button_matches(trigger: TriggerKey, button: Button) -> bool {
    match (trigger, button) {
        (TriggerKey::MouseLeft, Button::Left)
        | (TriggerKey::MouseRight, Button::Right)
        | (TriggerKey::MouseMiddle, Button::Middle) => true,
        // Windows reports XBUTTON1/XBUTTON2 as Unknown(1)/Unknown(2), i.e. Mouse 4/5
        (TriggerKey::MouseButton(n), Button::Unknown(code)) => {
            n >= 4 && u16::from(n) == u16::from(code) + 3
        }
        _ => false,
    }
}

macro_rules! vk_table {
    ($($key:ident => $code:literal),* $(,)?) => {
        /// Windows virtual-key code of an rdev key (same table rdev uses internally).
        /// Unnamed keys arrive as `Key::Unknown(vk)`.
        pub fn vk_of(key: Key) -> Option<u16> {
            match key {
                $(Key::$key => Some($code),)*
                Key::Unknown(code) => u16::try_from(code).ok(),
                _ => None,
            }
        }
    };
}

vk_table! {
    Alt => 164, AltGr => 165, Backspace => 8, CapsLock => 20, ControlLeft => 162, ControlRight => 163,
    Delete => 46, DownArrow => 40, End => 35, Escape => 27,
    F1 => 112, F2 => 113, F3 => 114, F4 => 115, F5 => 116, F6 => 117, F7 => 118, F8 => 119, F9 => 120,
    F10 => 121, F11 => 122, F12 => 123,
    Home => 36, LeftArrow => 37, MetaLeft => 91, PageDown => 34, PageUp => 33, Return => 13, RightArrow => 39,
    ShiftLeft => 160, ShiftRight => 161, Space => 32, Tab => 9, UpArrow => 38, PrintScreen => 44,
    ScrollLock => 145, Pause => 19, NumLock => 144, BackQuote => 192,
    Num1 => 49, Num2 => 50, Num3 => 51, Num4 => 52, Num5 => 53, Num6 => 54, Num7 => 55, Num8 => 56,
    Num9 => 57, Num0 => 48, Minus => 189, Equal => 187,
    KeyQ => 81, KeyW => 87, KeyE => 69, KeyR => 82, KeyT => 84, KeyY => 89, KeyU => 85, KeyI => 73,
    KeyO => 79, KeyP => 80, LeftBracket => 219, RightBracket => 221,
    KeyA => 65, KeyS => 83, KeyD => 68, KeyF => 70, KeyG => 71, KeyH => 72, KeyJ => 74, KeyK => 75,
    KeyL => 76, SemiColon => 186, Quote => 222, BackSlash => 220, IntlBackslash => 226,
    KeyZ => 90, KeyX => 88, KeyC => 67, KeyV => 86, KeyB => 66, KeyN => 78, KeyM => 77,
    Comma => 188, Dot => 190, Slash => 191, Insert => 45,
    KpMinus => 109, KpPlus => 107, KpMultiply => 106, KpDivide => 111,
    Kp0 => 96, Kp1 => 97, Kp2 => 98, Kp3 => 99, Kp4 => 100, Kp5 => 101, Kp6 => 102, Kp7 => 103,
    Kp8 => 104, Kp9 => 105, KpDelete => 110,
}

// capture: grab the next key or mouse button globally
struct Capture {
    active: bool,
    result: Option<String>,
}

static CAPTURE: Mutex<Capture> = Mutex::new(Capture {
    active: false,
    result: None,
});

/// Trigger name of a key: a known name (e.g. `Space`), otherwise `VK<code>`.
pub fn key_token(key: Key) -> String {
    if let Some(known) = TriggerKey::ALL
        .iter()
        .copied()
        .filter(|t| !t.is_mouse())
        .find(|&t| key_matches(t, key))
    {
        return known.name().into_owned();
    }
    match vk_of(key) {
        Some(code) => format!("VK{code}"),
        None => format!("{key:?}"), // e.g. MetaRight can't be used as a trigger
    }
}

/// Trigger name of a mouse button (`MouseLeft`, ..., `Mouse4`, `Mouse5`, ...).
pub fn button_token(button: Button) -> String {
    match button {
        Button::Left => "MouseLeft".to_string(),
        Button::Right => "MouseRight".to_string(),
        Button::Middle => "MouseMiddle".to_string(),
        Button::Unknown(code) => format!("Mouse{}", u16::from(code) + 3),
    }
}

fn capture_token(event: &EventType) -> Option<String> {
    match *event {
        EventType::KeyPress(key) => Some(key_token(key)),
        EventType::ButtonPress(button) => Some(button_token(button)),
        _ => None,
    }
}

/// The next key press or click anywhere on the system is captured (and not counted as
/// Morse input). Read the result with [`take_capture`].
pub fn begin_capture() -> io::Result<()> {
    ensure_hook_thread()?;
    let mut capture = lock_or_recover(&CAPTURE);
    capture.active = true;
    capture.result = None;
    Ok(())
}

pub fn cancel_capture() {
    let mut capture = lock_or_recover(&CAPTURE);
    capture.active = false;
    capture.result = None;
}

/// Returns the captured trigger name once, as soon as there is one.
pub fn take_capture() -> Option<String> {
    lock_or_recover(&CAPTURE).result.take()
}

/// Callback of the hook thread. Returns the event to let it through, `None` to swallow it.
fn handle_event(event: rdev::Event) -> Option<rdev::Event> {
    // take the timestamp right away, and monotonic (`Instant`): `event.time` is wall
    // clock time and can jump with NTP corrections
    let now = Instant::now();

    // fast exit for the vast majority of events (mouse moves and so on)
    if matches!(
        event.event_type,
        EventType::MouseMove { .. } | EventType::Wheel { .. }
    ) {
        return Some(event);
    }

    match event.event_type {
        EventType::KeyPress(key) => {
            if let Some(bit) = modifier_bit(key) {
                MODIFIERS.fetch_or(bit, Ordering::SeqCst);
            }
        }
        EventType::KeyRelease(key) => {
            if let Some(bit) = modifier_bit(key) {
                MODIFIERS.fetch_and(!bit, Ordering::SeqCst);
            }
        }
        _ => {}
    }

    // waiting for input: the next press belongs to the key binding, not to morsing
    if let Some(token) = capture_token(&event.event_type) {
        let mut capture = lock_or_recover(&CAPTURE);
        if capture.active {
            capture.active = false;
            capture.result = Some(token);
            return Some(event);
        }
    }

    // the shortcut toggles Morse input, even while our own typing is suppressed. Only the
    // first press counts: holding the key sends repeated presses that would flip it back
    // and forth
    if let Some(shortcut) = *lock_or_recover(&SHORTCUT) {
        match route_event(shortcut.key, &event.event_type) {
            Some(Edge::Press) => {
                if shortcut.modifiers_held(MODIFIERS.load(Ordering::SeqCst))
                    && !SHORTCUT_DOWN.swap(true, Ordering::SeqCst)
                {
                    ACTIVE.fetch_xor(true, Ordering::SeqCst);
                }
            }
            Some(Edge::Release) => SHORTCUT_DOWN.store(false, Ordering::SeqCst),
            None => {}
        }
    }

    if is_suppressed(now) {
        return Some(event);
    }

    let route = lock_or_recover(&ROUTE);
    let Some(route) = route.as_ref() else {
        return Some(event);
    };
    let Some(edge) = route_event(route.trigger, &event.event_type) else {
        return Some(event);
    };
    let message = match edge {
        Edge::Press => WorkerMessage::Press(now),
        Edge::Release => WorkerMessage::Release(now),
    };
    // an error means the worker ended, and clear_route removes the route soon
    let _ = route.tx.send(message);
    if should_swallow(route.trigger, edge) {
        None
    } else {
        Some(event)
    }
}

/// Decides whether a trigger event is hidden from other apps.
///
/// A swallowed press also swallows its release, so no app is left with a key stuck
/// down. The left mouse button is never swallowed, otherwise the app couldn't be
/// operated at all.
fn should_swallow(trigger: TriggerKey, edge: Edge) -> bool {
    if trigger == TriggerKey::MouseLeft {
        return false;
    }
    match edge {
        Edge::Press => {
            let block = BLOCK.load(Ordering::SeqCst) && ACTIVE.load(Ordering::SeqCst);
            PRESS_BLOCKED.store(block, Ordering::SeqCst);
            block
        }
        Edge::Release => PRESS_BLOCKED.swap(false, Ordering::SeqCst),
    }
}

/// Turns blocking of the trigger''s normal function on or off.
pub fn set_blocking(on: bool) {
    BLOCK.store(on, Ordering::SeqCst);
}

/// Whether blocking is switched on.
pub fn blocking() -> bool {
    BLOCK.load(Ordering::SeqCst)
}

/// Turns Morse input on or off (what the shortcut does too).
pub fn set_active(on: bool) {
    ACTIVE.store(on, Ordering::SeqCst);
}

/// Whether Morse input is on (the shortcut can flip it from the hook thread).
pub fn active() -> bool {
    ACTIVE.load(Ordering::SeqCst)
}

/// Sets the toggle shortcut; an empty string removes it.
pub fn set_shortcut(text: &str) -> Result<(), String> {
    *lock_or_recover(&SHORTCUT) = if text.trim().is_empty() {
        None
    } else {
        Some(parse_shortcut(text)?)
    };
    Ok(())
}

fn is_suppressed(now: Instant) -> bool {
    let guard = lock_or_recover(&SUPPRESS_UNTIL);
    guard.is_some_and(|until| now < until)
}

/// Starts the hook thread exactly once per process.
fn ensure_hook_thread() -> io::Result<()> {
    // compare_exchange instead of OnceLock so a failed spawn can be retried
    if HOOK_STARTED
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Ok(());
    }
    let spawned = thread::Builder::new()
        .name("morse-input-hook".into())
        .spawn(|| {
            // Windows can swallow events (grab); elsewhere the hook only observes
            #[cfg(windows)]
            let result = rdev::grab(handle_event);
            #[cfg(not(windows))]
            let result = rdev::listen(|event| {
                handle_event(event);
            });
            if let Err(err) = result {
                // typical causes: no X11 display on Linux, missing accessibility permission
                // on macOS. Python reads the error via `hook_error()` and shows it in the tray
                *lock_or_recover(&HOOK_ERROR) =
                    Some(format!("Globaler Input-Hook fehlgeschlagen: {err:?}"));
            }
        });
    if let Err(err) = spawned {
        HOOK_STARTED.store(false, Ordering::Release);
        return Err(err);
    }
    Ok(())
}

/// Connects the global hook to a worker.
pub fn install_route(trigger: TriggerKey, tx: Sender<WorkerMessage>) -> io::Result<()> {
    ensure_hook_thread()?;
    *lock_or_recover(&ROUTE) = Some(Route { trigger, tx });
    Ok(())
}

/// Disconnects the hook from the worker (the hook thread keeps running but does nothing).
pub fn clear_route() {
    *lock_or_recover(&ROUTE) = None;
}

/// Changes the trigger without rebuilding the route.
pub fn set_trigger(trigger: TriggerKey) {
    if let Some(route) = lock_or_recover(&ROUTE).as_mut() {
        route.trigger = trigger;
    }
}

/// Ignores trigger events for a short time.
///
/// If the trigger is the space bar and we inject a space for a word gap, our own hook
/// would take that synthetic space for a Morse dot. The injector calls this before and
/// after sending.
pub fn suppress_for(duration: Duration) {
    let until = Instant::now() + duration;
    let mut guard = lock_or_recover(&SUPPRESS_UNTIL);
    // only extend, never shorten
    if guard.is_none_or(|current| current < until) {
        *guard = Some(until);
    }
}

/// Last error of the hook thread, if `rdev::grab` failed.
pub fn hook_error() -> Option<String> {
    lock_or_recover(&HOOK_ERROR).clone()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The hook state is global, so tests that change it must not run in parallel.
    static GLOBAL_STATE: Mutex<()> = Mutex::new(());

    #[test]
    fn keyboard_trigger_maps_press_and_release() {
        assert_eq!(
            route_event(TriggerKey::F8, &EventType::KeyPress(Key::F8)),
            Some(Edge::Press)
        );
        assert_eq!(
            route_event(TriggerKey::F8, &EventType::KeyRelease(Key::F8)),
            Some(Edge::Release)
        );
    }

    #[test]
    fn other_keys_are_ignored() {
        assert_eq!(
            route_event(TriggerKey::F8, &EventType::KeyPress(Key::F9)),
            None
        );
        assert_eq!(
            route_event(TriggerKey::Space, &EventType::KeyPress(Key::KeyA)),
            None
        );
    }

    #[test]
    fn mouse_trigger_maps_buttons() {
        assert_eq!(
            route_event(
                TriggerKey::MouseRight,
                &EventType::ButtonPress(Button::Right)
            ),
            Some(Edge::Press)
        );
        assert_eq!(
            route_event(
                TriggerKey::MouseRight,
                &EventType::ButtonRelease(Button::Right)
            ),
            Some(Edge::Release)
        );
        assert_eq!(
            route_event(
                TriggerKey::MouseRight,
                &EventType::ButtonPress(Button::Left)
            ),
            None
        );
    }

    #[test]
    fn mouse_trigger_ignores_keyboard_and_vice_versa() {
        assert_eq!(
            route_event(TriggerKey::MouseLeft, &EventType::KeyPress(Key::Space)),
            None
        );
        assert_eq!(
            route_event(TriggerKey::Space, &EventType::ButtonPress(Button::Left)),
            None
        );
    }

    #[test]
    fn every_keyboard_trigger_has_a_mapping() {
        // guard against adding a trigger and forgetting its mapping: every keyboard
        // trigger must react to some key
        let candidates = [
            Key::ScrollLock,
            Key::Pause,
            Key::Insert,
            Key::ControlRight,
            Key::AltGr,
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
            Key::Space,
        ];
        for &trigger in TriggerKey::ALL.iter().filter(|t| !t.is_mouse()) {
            let hits = candidates
                .iter()
                .filter(|&&k| key_matches(trigger, k))
                .count();
            assert_eq!(hits, 1, "{trigger} muss genau einer Taste zugeordnet sein");
        }
    }

    #[test]
    fn any_key_matches_by_virtual_key_code() {
        // Z (VK 90) isn't a standard trigger but works through VK90
        let trigger = TriggerKey::Vk(90);
        assert_eq!(
            route_event(trigger, &EventType::KeyPress(Key::KeyZ)),
            Some(Edge::Press)
        );
        assert_eq!(
            route_event(trigger, &EventType::KeyRelease(Key::KeyZ)),
            Some(Edge::Release)
        );
        assert_eq!(route_event(trigger, &EventType::KeyPress(Key::KeyY)), None);
        // keys without an rdev name arrive as Unknown(vk), e.g. F13 = 124
        assert_eq!(
            route_event(TriggerKey::Vk(124), &EventType::KeyPress(Key::Unknown(124))),
            Some(Edge::Press)
        );
    }

    #[test]
    fn extra_mouse_buttons_four_and_five() {
        let four = TriggerKey::MouseButton(4);
        let five = TriggerKey::MouseButton(5);
        assert_eq!(
            route_event(four, &EventType::ButtonPress(Button::Unknown(1))),
            Some(Edge::Press)
        );
        assert_eq!(
            route_event(five, &EventType::ButtonPress(Button::Unknown(2))),
            Some(Edge::Press)
        );
        assert_eq!(
            route_event(five, &EventType::ButtonRelease(Button::Unknown(2))),
            Some(Edge::Release)
        );
        assert_eq!(
            route_event(four, &EventType::ButtonPress(Button::Unknown(2))),
            None
        );
        assert_eq!(
            route_event(five, &EventType::ButtonPress(Button::Left)),
            None
        );
    }

    #[test]
    fn tokens_name_inputs_in_the_trigger_syntax() {
        assert_eq!(key_token(Key::Space), "Space");
        assert_eq!(key_token(Key::KeyA), "VK65");
        assert_eq!(key_token(Key::Unknown(124)), "VK124");
        assert_eq!(button_token(Button::Right), "MouseRight");
        assert_eq!(button_token(Button::Unknown(1)), "Mouse4");
        assert_eq!(button_token(Button::Unknown(2)), "Mouse5");
        // every generated name parses as a trigger
        for token in [
            key_token(Key::KeyA),
            button_token(Button::Unknown(2)),
            key_token(Key::F9),
        ] {
            assert!(token.parse::<TriggerKey>().is_ok(), "{token}");
        }
    }

    #[test]
    fn capture_takes_next_press_once_and_does_not_route() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        let (tx, rx) = std::sync::mpsc::channel();
        *lock_or_recover(&ROUTE) = Some(Route {
            trigger: TriggerKey::F12,
            tx,
        });
        *lock_or_recover(&SUPPRESS_UNTIL) = None;
        {
            let mut capture = lock_or_recover(&CAPTURE);
            capture.active = true;
            capture.result = None;
        }
        let event = |event_type| rdev::Event {
            time: std::time::SystemTime::now(),
            name: None,
            event_type,
        };
        handle_event(event(EventType::KeyRelease(Key::F12))); // releasing doesn't count
        assert_eq!(take_capture(), None);
        handle_event(event(EventType::KeyPress(Key::F12)));
        assert_eq!(take_capture().as_deref(), Some("F12"));
        assert_eq!(take_capture(), None);
        // after that the trigger works normally again; only the release (before the
        // capture) and the last press were routed, the captured press wasn't
        handle_event(event(EventType::KeyPress(Key::F12)));
        clear_route();
        let kinds: Vec<_> = rx
            .try_iter()
            .map(|m| matches!(m, WorkerMessage::Press(_)))
            .collect();
        assert_eq!(kinds, vec![false, true]);
    }

    /// Puts the global hook state into a known state for the blocking tests.
    fn reset_block_state() {
        BLOCK.store(false, Ordering::SeqCst);
        ACTIVE.store(true, Ordering::SeqCst);
        PRESS_BLOCKED.store(false, Ordering::SeqCst);
        SHORTCUT_DOWN.store(false, Ordering::SeqCst);
        MODIFIERS.store(0, Ordering::SeqCst);
        *lock_or_recover(&SHORTCUT) = None;
        *lock_or_recover(&SUPPRESS_UNTIL) = None;
        *lock_or_recover(&CAPTURE) = Capture {
            active: false,
            result: None,
        };
    }

    fn press(key: Key) -> rdev::Event {
        rdev::Event {
            time: std::time::SystemTime::now(),
            name: None,
            event_type: EventType::KeyPress(key),
        }
    }

    fn release(key: Key) -> rdev::Event {
        rdev::Event {
            event_type: EventType::KeyRelease(key),
            ..press(key)
        }
    }

    #[test]
    fn shortcut_parsing() {
        let sc = parse_shortcut("Ctrl+Shift+Pause").unwrap();
        assert!(sc.ctrl && sc.shift && !sc.alt && !sc.meta);
        assert_eq!(sc.key, TriggerKey::Pause);
        assert_eq!(
            parse_shortcut("alt + VK77").unwrap().key,
            TriggerKey::Vk(77)
        );
        assert_eq!(parse_shortcut("F9").unwrap().key, TriggerKey::F9);
        assert!(parse_shortcut("Hyper+F9").is_err());
        assert!(parse_shortcut("Ctrl+").is_err());
        assert!(parse_shortcut("").is_err());
    }

    #[test]
    fn shortcut_toggles_morse_input_only_with_its_modifiers() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        set_shortcut("Ctrl+Shift+Pause").unwrap();
        handle_event(press(Key::Pause)); // no modifiers held
        assert!(active());
        handle_event(release(Key::Pause));
        handle_event(press(Key::ControlLeft));
        handle_event(press(Key::Pause)); // shift is missing
        assert!(active());
        handle_event(release(Key::Pause));
        handle_event(press(Key::ShiftRight));
        handle_event(press(Key::Pause));
        assert!(!active());
        handle_event(release(Key::Pause));
        handle_event(press(Key::Pause)); // pressed again: back on
        assert!(active());
        handle_event(release(Key::Pause));
        handle_event(release(Key::ControlLeft));
        handle_event(release(Key::ShiftRight));
        handle_event(press(Key::Pause));
        assert!(active(), "released modifiers no longer count");
        set_shortcut("").unwrap();
        reset_block_state();
    }

    #[test]
    fn holding_the_shortcut_toggles_only_once() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        set_shortcut("Ctrl+Shift+Pause").unwrap();
        handle_event(press(Key::ControlLeft));
        handle_event(press(Key::ShiftLeft));
        for _ in 0..5 {
            handle_event(press(Key::Pause)); // key repeat while held
        }
        assert!(!active(), "five repeats are one press");
        handle_event(release(Key::Pause));
        handle_event(press(Key::Pause));
        assert!(active(), "a new press toggles again");
        set_shortcut("").unwrap();
        reset_block_state();
    }

    #[test]
    fn blocking_stops_while_morse_input_is_off() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let (tx, _rx) = std::sync::mpsc::channel();
        *lock_or_recover(&ROUTE) = Some(Route {
            trigger: TriggerKey::Space,
            tx,
        });
        set_blocking(true);
        assert!(handle_event(press(Key::Space)).is_none());
        handle_event(release(Key::Space));
        set_active(false);
        assert!(
            handle_event(press(Key::Space)).is_some(),
            "Morse is off, the key works normally"
        );
        handle_event(release(Key::Space));
        set_active(true);
        assert!(handle_event(press(Key::Space)).is_none());
        clear_route();
        reset_block_state();
    }
    #[test]
    fn blocking_swallows_trigger_press_and_its_release_only() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let (tx, rx) = std::sync::mpsc::channel();
        *lock_or_recover(&ROUTE) = Some(Route {
            trigger: TriggerKey::Space,
            tx,
        });
        assert!(
            handle_event(press(Key::Space)).is_some(),
            "not blocking yet"
        );
        set_blocking(true);
        assert!(
            handle_event(release(Key::Space)).is_some(),
            "press wasn't swallowed, so neither is the release"
        );
        assert!(handle_event(press(Key::Space)).is_none());
        assert!(handle_event(press(Key::Space)).is_none(), "key repeat too");
        assert!(
            handle_event(press(Key::KeyA)).is_some(),
            "other keys are untouched"
        );
        set_blocking(false);
        assert!(
            handle_event(release(Key::Space)).is_none(),
            "no stuck key after switching off"
        );
        assert!(handle_event(press(Key::Space)).is_some());
        clear_route();
        assert_eq!(
            rx.try_iter().count(),
            6,
            "blocked keys still reach the engine"
        );
        reset_block_state();
    }

    #[test]
    fn left_mouse_button_is_never_swallowed() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let (tx, _rx) = std::sync::mpsc::channel();
        *lock_or_recover(&ROUTE) = Some(Route {
            trigger: TriggerKey::MouseLeft,
            tx,
        });
        set_blocking(true);
        let click = rdev::Event {
            event_type: EventType::ButtonPress(Button::Left),
            ..press(Key::Space)
        };
        assert!(handle_event(click).is_some());
        clear_route();
        reset_block_state();
    }

    #[test]
    fn blocking_without_a_route_lets_everything_through() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        clear_route();
        set_blocking(true);
        assert!(handle_event(press(Key::Space)).is_some());
        reset_block_state();
    }

    #[test]
    fn suppression_window() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        // uses the global state, so only relative checks
        suppress_for(Duration::from_millis(200));
        assert!(is_suppressed(Instant::now()));
        assert!(!is_suppressed(Instant::now() + Duration::from_secs(1)));
    }

    #[test]
    fn route_forwards_to_worker_channel() {
        // call the callback directly with a synthetic event, without a real hook
        // thread (ensure_hook_thread isn't used here)
        let _serial = lock_or_recover(&GLOBAL_STATE);
        let (tx, rx) = std::sync::mpsc::channel();
        *lock_or_recover(&ROUTE) = Some(Route {
            trigger: TriggerKey::F12,
            tx,
        });
        *lock_or_recover(&SUPPRESS_UNTIL) = None;
        handle_event(rdev::Event {
            time: std::time::SystemTime::now(),
            name: None,
            event_type: EventType::KeyPress(Key::F12),
        });
        handle_event(rdev::Event {
            time: std::time::SystemTime::now(),
            name: None,
            event_type: EventType::KeyPress(Key::KeyQ),
        });
        clear_route();
        let received: Vec<_> = rx.try_iter().collect();
        assert_eq!(received.len(), 1);
        assert!(matches!(received[0], WorkerMessage::Press(_)));
    }
}
