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
use std::sync::atomic::{AtomicBool, AtomicU16, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use rdev::{Button, EventType, Key};

use crate::engine::WorkerMessage;
use crate::lock_or_recover;
use crate::timing::Symbol;
use crate::trigger::{modifier_names, KeyBinding, ModifierMask, MorseKeys, TriggerKey, CAPS_LOCK};

/// Edge of a trigger.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Edge {
    Press,
    Release,
}

/// One routed key per Morse key; with a separate dash key each gets its fixed symbol.
fn routed(keys: MorseKeys) -> Vec<RoutedKey> {
    match keys.dash_key {
        None => vec![RoutedKey::new(keys.key, None)],
        Some(dash_key) => vec![
            RoutedKey::new(keys.key, Some(Symbol::Dot)),
            RoutedKey::new(dash_key, Some(Symbol::Dash)),
        ],
    }
}

/// A Morse key together with the state of its current press.
struct RoutedKey {
    binding: KeyBinding,
    /// Fixed symbol of a separate dot or dash key, `None` = decided by press duration.
    symbol: Option<Symbol>,
    /// The press was accepted (modifiers were held), so its release counts too.
    down: bool,
    /// The press was swallowed, so its release is swallowed too (no stuck key).
    swallowed: bool,
}

impl RoutedKey {
    fn new(binding: KeyBinding, symbol: Option<Symbol>) -> Self {
        Self {
            binding,
            symbol,
            down: false,
            swallowed: false,
        }
    }

    /// Message for the worker, or `None` if the edge doesn't count (a modifier is
    /// missing, or a release without an accepted press).
    fn message(&mut self, edge: Edge, held: ModifierMask, at: Instant) -> Option<WorkerMessage> {
        match edge {
            Edge::Press => {
                // key repeat of an accepted press counts even if a modifier was let go meanwhile
                if !self.down && !self.binding.modifiers_held(held) {
                    return None;
                }
                self.down = true;
                Some(WorkerMessage::Press(at))
            }
            Edge::Release => {
                if !std::mem::take(&mut self.down) {
                    return None;
                }
                Some(match self.symbol {
                    Some(symbol) => WorkerMessage::ReleaseAs(at, symbol),
                    None => WorkerMessage::Release(at),
                })
            }
        }
    }

    /// Decides whether an accepted edge is hidden from other apps.
    ///
    /// A swallowed press also swallows its release, so no app is left with a key stuck
    /// down. The left mouse button is never swallowed, otherwise the app couldn't be
    /// operated at all.
    fn swallow(&mut self, edge: Edge) -> bool {
        if self.binding.key == TriggerKey::MouseLeft {
            return false;
        }
        match edge {
            Edge::Press => {
                self.swallowed = blocking_now();
                self.swallowed
            }
            Edge::Release => std::mem::take(&mut self.swallowed),
        }
    }
}

/// Where trigger events go.
struct Route {
    keys: Vec<RoutedKey>,
    tx: Sender<WorkerMessage>,
}

impl Route {
    /// Caps Lock used as a modifier of a Morse key would flip the caps state with every
    /// symbol, so while blocking is on it is swallowed like the Morse keys themselves.
    fn swallows_caps_lock(&self, event: &EventType) -> bool {
        matches!(
            event,
            EventType::KeyPress(Key::CapsLock) | EventType::KeyRelease(Key::CapsLock)
        ) && blocking_now()
            && self.keys.iter().any(|k| k.binding.requires_caps_lock())
    }
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
/// The shortcut key is held, so key repeat doesn't toggle blocking again and again.
static SHORTCUT_DOWN: AtomicBool = AtomicBool::new(false);
/// Modifier keys currently held, one bit per physical key (see `modifier_bit`).
static MODIFIERS: AtomicU16 = AtomicU16::new(0);
/// The on/off shortcut, e.g. `Ctrl+Shift+Pause`.
static SHORTCUT: Mutex<Option<KeyBinding>> = Mutex::new(None);

/// Bit of a modifier key in `MODIFIERS`, matching the group masks in `trigger`.
fn modifier_bit(key: Key) -> Option<ModifierMask> {
    match key {
        Key::ControlLeft => Some(1),
        Key::ControlRight => Some(2),
        Key::Alt => Some(4),
        Key::AltGr => Some(8),
        Key::ShiftLeft => Some(16),
        Key::ShiftRight => Some(32),
        Key::MetaLeft => Some(64),
        Key::Unknown(92) => Some(128), // right Windows key
        Key::CapsLock => Some(CAPS_LOCK),
        _ => None,
    }
}

/// Blocking is switched on and Morse input is active.
fn blocking_now() -> bool {
    BLOCK.load(Ordering::SeqCst) && ACTIVE.load(Ordering::SeqCst)
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

// capture: grab the next key, mouse button or combination globally
struct Capture {
    active: bool,
    result: Option<String>,
    /// Modifier pressed while waiting. If it is released before any other key, the
    /// modifier itself is the result (e.g. Right Ctrl as the Morse key).
    lone_modifier: Option<Key>,
}

impl Capture {
    /// Feeds one event while waiting. A key or button pressed while modifiers are held
    /// gives a combination such as `CapsLock+VK81`.
    fn feed(&mut self, event: &EventType, held: ModifierMask) {
        let token = match *event {
            EventType::KeyPress(key) if modifier_bit(key).is_some() => {
                self.lone_modifier = Some(key);
                return;
            }
            EventType::KeyRelease(key) if self.lone_modifier == Some(key) => key_token(key),
            EventType::KeyPress(key) => with_modifiers(held, &key_token(key)),
            EventType::ButtonPress(button) => with_modifiers(held, &button_token(button)),
            _ => return,
        };
        self.active = false;
        self.lone_modifier = None;
        self.result = Some(token);
    }
}

fn with_modifiers(held: ModifierMask, token: &str) -> String {
    modifier_names(held)
        .into_iter()
        .chain([token])
        .collect::<Vec<_>>()
        .join("+")
}

static CAPTURE: Mutex<Capture> = Mutex::new(Capture {
    active: false,
    result: None,
    lone_modifier: None,
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

/// The next key press, click or combination anywhere on the system is captured (and not
/// counted as Morse input). Read the result with [`take_capture`].
pub fn begin_capture() -> io::Result<()> {
    ensure_hook_thread()?;
    let mut capture = lock_or_recover(&CAPTURE);
    capture.active = true;
    capture.result = None;
    capture.lone_modifier = None;
    Ok(())
}

pub fn cancel_capture() {
    let mut capture = lock_or_recover(&CAPTURE);
    capture.active = false;
    capture.result = None;
    capture.lone_modifier = None;
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

    let held = MODIFIERS.load(Ordering::SeqCst);

    // waiting for input: everything belongs to the key binding, nothing to morsing
    {
        let mut capture = lock_or_recover(&CAPTURE);
        if capture.active {
            capture.feed(&event.event_type, held);
            return Some(event);
        }
    }

    // the shortcut toggles Morse input, even while our own typing is suppressed. Only the
    // first press counts: holding the key sends repeated presses that would flip it back
    // and forth
    if let Some(shortcut) = *lock_or_recover(&SHORTCUT) {
        match route_event(shortcut.key, &event.event_type) {
            Some(Edge::Press) => {
                if shortcut.modifiers_held(held) && !SHORTCUT_DOWN.swap(true, Ordering::SeqCst) {
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

    let mut guard = lock_or_recover(&ROUTE);
    let Some(route) = guard.as_mut() else {
        return Some(event);
    };
    if route.swallows_caps_lock(&event.event_type) {
        return None;
    }
    let Route { keys, tx } = route;
    for key in keys.iter_mut() {
        let Some(edge) = route_event(key.binding.key, &event.event_type) else {
            continue;
        };
        let Some(message) = key.message(edge, held, now) else {
            continue;
        };
        // an error means the worker ended, and clear_route removes the route soon
        let _ = tx.send(message);
        return if key.swallow(edge) { None } else { Some(event) };
    }
    Some(event)
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
        Some(text.parse::<KeyBinding>()?)
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
pub fn install_route(keys: MorseKeys, tx: Sender<WorkerMessage>) -> io::Result<()> {
    ensure_hook_thread()?;
    *lock_or_recover(&ROUTE) = Some(Route {
        keys: routed(keys),
        tx,
    });
    Ok(())
}

/// Disconnects the hook from the worker (the hook thread keeps running but does nothing).
pub fn clear_route() {
    *lock_or_recover(&ROUTE) = None;
}

/// Changes the Morse keys without rebuilding the route.
pub fn set_keys(keys: MorseKeys) {
    if let Some(route) = lock_or_recover(&ROUTE).as_mut() {
        route.keys = routed(keys);
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

    /// Routes `key` (and an optional separate dash key) to a fresh channel.
    fn route_keys(key: &str, dash_key: Option<&str>) -> std::sync::mpsc::Receiver<WorkerMessage> {
        let (tx, rx) = std::sync::mpsc::channel();
        let keys = MorseKeys {
            key: key.parse().unwrap(),
            dash_key: dash_key.map(|k| k.parse().unwrap()),
        };
        *lock_or_recover(&ROUTE) = Some(Route {
            keys: routed(keys),
            tx,
        });
        rx
    }

    #[test]
    fn capture_takes_next_press_once_and_does_not_route() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let rx = route_keys("F12", None);
        begin_test_capture();
        handle_event(release(Key::F12)); // releasing doesn't count
        assert_eq!(take_capture(), None);
        handle_event(press(Key::F12));
        assert_eq!(take_capture().as_deref(), Some("F12"));
        assert_eq!(take_capture(), None);
        // after that the trigger works normally again; the captured press wasn't routed
        handle_event(press(Key::F12));
        clear_route();
        let presses = rx
            .try_iter()
            .filter(|m| matches!(m, WorkerMessage::Press(_)))
            .count();
        assert_eq!(presses, 1);
        reset_block_state();
    }

    #[test]
    fn capture_reports_combinations_and_lone_modifiers() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        begin_test_capture();
        handle_event(press(Key::CapsLock));
        handle_event(press(Key::KeyQ));
        assert_eq!(take_capture().as_deref(), Some("CapsLock+VK81"));
        handle_event(release(Key::KeyQ));
        handle_event(release(Key::CapsLock));

        begin_test_capture();
        handle_event(press(Key::ControlLeft));
        handle_event(press(Key::ShiftLeft));
        handle_event(press(Key::Pause));
        assert_eq!(take_capture().as_deref(), Some("Ctrl+Shift+Pause"));
        handle_event(release(Key::ShiftLeft));
        handle_event(release(Key::ControlLeft));

        // a modifier pressed and released on its own is the key itself
        begin_test_capture();
        handle_event(press(Key::ControlRight));
        assert_eq!(take_capture(), None, "might still become Ctrl+something");
        handle_event(release(Key::ControlRight));
        assert_eq!(take_capture().as_deref(), Some("ControlRight"));
        reset_block_state();
    }

    fn begin_test_capture() {
        let mut capture = lock_or_recover(&CAPTURE);
        capture.active = true;
        capture.result = None;
        capture.lone_modifier = None;
    }

    #[test]
    fn combination_key_counts_only_with_its_modifier() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let rx = route_keys("CapsLock+VK81", None);
        handle_event(press(Key::KeyQ)); // plain Q: normal typing, not Morse
        handle_event(release(Key::KeyQ));
        handle_event(press(Key::CapsLock));
        handle_event(press(Key::KeyQ));
        handle_event(release(Key::CapsLock)); // let go of Caps Lock first ...
        handle_event(press(Key::KeyQ)); // ... key repeat still belongs to the accepted press
        handle_event(release(Key::KeyQ)); // ... and the release still ends it
        clear_route();
        let kinds: Vec<_> = rx
            .try_iter()
            .map(|m| match m {
                WorkerMessage::Press(_) => "press",
                WorkerMessage::Release(_) => "release",
                _ => "other",
            })
            .collect();
        assert_eq!(kinds, vec!["press", "press", "release"]);
        reset_block_state();
    }

    #[test]
    fn separate_keys_send_fixed_symbols() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let rx = route_keys("F11", Some("F12"));
        for key in [Key::F11, Key::F12] {
            handle_event(press(key));
            handle_event(release(key));
        }
        clear_route();
        let symbols: Vec<_> = rx
            .try_iter()
            .filter_map(|m| match m {
                WorkerMessage::ReleaseAs(_, symbol) => Some(symbol),
                _ => None,
            })
            .collect();
        assert_eq!(symbols, vec![Symbol::Dot, Symbol::Dash]);
        reset_block_state();
    }

    #[test]
    fn caps_lock_is_swallowed_only_when_blocking_a_caps_combination() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        route_keys("CapsLock+VK81", None);
        assert!(
            handle_event(press(Key::CapsLock)).is_some(),
            "not blocking yet"
        );
        handle_event(release(Key::CapsLock));
        set_blocking(true);
        assert!(
            handle_event(press(Key::CapsLock)).is_none(),
            "would flip caps on every symbol"
        );
        assert!(handle_event(release(Key::CapsLock)).is_none());
        route_keys("F12", None);
        assert!(
            handle_event(press(Key::CapsLock)).is_some(),
            "no caps combination in use"
        );
        handle_event(release(Key::CapsLock));
        clear_route();
        reset_block_state();
    }

    /// Puts the global hook state into a known state for the blocking tests.
    fn reset_block_state() {
        BLOCK.store(false, Ordering::SeqCst);
        ACTIVE.store(true, Ordering::SeqCst);
        SHORTCUT_DOWN.store(false, Ordering::SeqCst);
        MODIFIERS.store(0, Ordering::SeqCst);
        *lock_or_recover(&SHORTCUT) = None;
        *lock_or_recover(&SUPPRESS_UNTIL) = None;
        *lock_or_recover(&CAPTURE) = Capture {
            active: false,
            result: None,
            lone_modifier: None,
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
        let _rx = route_keys("Space", None);
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
        let rx = route_keys("Space", None);
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
        let _rx = route_keys("MouseLeft", None);
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
        reset_block_state();
        let rx = route_keys("F12", None);
        handle_event(press(Key::F12));
        handle_event(press(Key::KeyQ));
        clear_route();
        let received: Vec<_> = rx.try_iter().collect();
        assert_eq!(received.len(), 1);
        assert!(matches!(received[0], WorkerMessage::Press(_)));
    }
}
