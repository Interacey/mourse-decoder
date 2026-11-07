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
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU32, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use rdev::{Button, EventType, Key};

use crate::engine::WorkerMessage;
use crate::function_keys::{self, Reserved};
use crate::lock_or_recover;
use crate::timing::Symbol;
use crate::trigger::{
    modifier_names, KeyBinding, ModifierMask, MorseKeys, TriggerKey, ALT, CAPS_LOCK, CTRL, SHIFT,
    WIN,
};

/// Edge of a trigger.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Edge {
    Press,
    Release,
}

/// One routed key per Morse key; with a separate dash key each gets its fixed symbol.
/// The binding with more required modifiers comes first, so `F8` as dot and `Ctrl+F8` as dash
/// both work (a binding matches with extra modifiers held, the first match wins).
fn routed(keys: MorseKeys) -> Vec<RoutedKey> {
    let mut out = match keys.dash_key {
        None => vec![RoutedKey::new(keys.key, None)],
        Some(dash_key) => vec![
            RoutedKey::new(keys.key, Some(Symbol::Dot)),
            RoutedKey::new(dash_key, Some(Symbol::Dash)),
        ],
    };
    out.sort_by_key(|k| std::cmp::Reverse(k.binding.specificity()));
    out
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
    /// The press belongs to this key but was refused (see [`Self::message`]); its repeats
    /// and its release are refused too.
    ignored: bool,
}

/// What a key edge means for the worker.
enum Verdict {
    Send(WorkerMessage),
    /// The edge belongs to this key but does not count; it is still hidden or not like any other.
    Skip,
}

impl RoutedKey {
    fn new(binding: KeyBinding, symbol: Option<Symbol>) -> Self {
        Self {
            binding,
            symbol,
            down: false,
            swallowed: false,
            ignored: false,
        }
    }

    /// Verdict for an edge of this key, or `None` if it is not ours (a modifier is missing,
    /// or a release without a press).
    ///
    /// `refuse_new` is true while a new press must not start: the other Morse key is held
    /// (the timer has one press state, so a rolled second key would be read as a repeat and
    /// its release as the wrong symbol), or our own typing is in flight.
    fn message(
        &mut self,
        edge: Edge,
        held: ModifierMask,
        at: Instant,
        refuse_new: bool,
    ) -> Option<Verdict> {
        match edge {
            Edge::Press => {
                // key repeat of an accepted press counts even if a modifier was let go meanwhile
                if self.down {
                    return Some(Verdict::Send(WorkerMessage::Press(at)));
                }
                if self.ignored {
                    return Some(Verdict::Skip);
                }
                if !self.binding.modifiers_held(held) {
                    return None;
                }
                if refuse_new {
                    self.ignored = true;
                    return Some(Verdict::Skip);
                }
                self.down = true;
                Some(Verdict::Send(WorkerMessage::Press(at)))
            }
            Edge::Release => {
                if std::mem::take(&mut self.down) {
                    Some(Verdict::Send(match self.symbol {
                        Some(symbol) => WorkerMessage::ReleaseAs(at, symbol),
                        None => WorkerMessage::Release(at),
                    }))
                } else if std::mem::take(&mut self.ignored) {
                    Some(Verdict::Skip)
                } else {
                    None
                }
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
/// A mask key (VK 0xE8) was sent since Alt/Win were last pressed (see `needs_menu_mask`).
static MASK_SENT: AtomicBool = AtomicBool::new(false);
#[cfg(test)]
static TEST_MASKS: AtomicU32 = AtomicU32::new(0);
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
/// The shortcut that switches to the next alphabet of the user's list, and how often it fired.
/// Python polls the counter like it polls `ACTIVE`.
static CYCLE_SHORTCUT: Mutex<Option<KeyBinding>> = Mutex::new(None);
static CYCLE_DOWN: AtomicBool = AtomicBool::new(false);
static CYCLE_COUNT: AtomicU32 = AtomicU32::new(0);
/// The shortcut that undoes the last typed output; same scheme as the cycle shortcut.
static UNDO_SHORTCUT: Mutex<Option<KeyBinding>> = Mutex::new(None);
static UNDO_DOWN: AtomicBool = AtomicBool::new(false);
static UNDO_COUNT: AtomicU32 = AtomicU32::new(0);

/// `Some(true)` on the first press of a shortcut's key while its modifiers are held,
/// `Some(false)` on its key repeats (`down` remembers the press, so they must not count
/// again), `None` for everything else. The caller keeps `Some` events away from the Morse keys.
fn shortcut_pressed(
    slot: &Mutex<Option<KeyBinding>>,
    down: &AtomicBool,
    event_type: &EventType,
    held: ModifierMask,
) -> Option<bool> {
    let shortcut = (*lock_or_recover(slot))?;
    match route_event(shortcut.key, event_type) {
        Some(Edge::Press) => shortcut
            .modifiers_held(held)
            .then(|| !down.swap(true, Ordering::SeqCst)),
        Some(Edge::Release) => {
            down.store(false, Ordering::SeqCst);
            None
        }
        None => None,
    }
}

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

/// Virtual-key codes of the modifier keys behind the bits of `modifier_bit`.
const MODIFIER_KEY_CODES: [(ModifierMask, i32); 8] = [
    (1, 0xA2),   // left Ctrl
    (2, 0xA3),   // right Ctrl
    (4, 0xA4),   // left Alt
    (8, 0xA5),   // right Alt / AltGr
    (16, 0xA0),  // left Shift
    (32, 0xA1),  // right Shift
    (64, 0x5B),  // left Windows key
    (128, 0x5C), // right Windows key
];

/// `held` corrected by what the keyboard really reports (`is_down` per virtual-key code).
/// The hook only sees key events, so a modifier released while another desktop had the
/// input (Win+L, Ctrl+Alt+Del, UAC) would stay "held" forever.
#[cfg_attr(not(all(windows, not(test))), allow(dead_code))]
fn sync_modifiers(held: ModifierMask, is_down: impl Fn(i32) -> bool) -> ModifierMask {
    MODIFIER_KEY_CODES.iter().fold(
        held,
        |held, &(bit, vk)| {
            if is_down(vk) {
                held | bit
            } else {
                held & !bit
            }
        },
    )
}

/// Re-reads the modifier keys from the system (Windows); a no-op elsewhere and in unit
/// tests, where the real keyboard must not interfere with synthetic events.
fn resync_modifiers() {
    #[cfg(all(windows, not(test)))]
    {
        use windows::Win32::UI::Input::KeyboardAndMouse::GetAsyncKeyState;
        let _ = MODIFIERS.fetch_update(Ordering::SeqCst, Ordering::SeqCst, |held| {
            // SAFETY: GetAsyncKeyState has no preconditions
            Some(sync_modifiers(
                held,
                |vk| unsafe { GetAsyncKeyState(vk) } < 0,
            ))
        });
    }
}

/// True if a swallowed key press needs a mask key: with Alt or Win held and the key hidden,
/// Windows would see a lone Alt/Win tap on release (menu bar, Start menu). Once per hold.
fn needs_menu_mask(held: ModifierMask, already_sent: bool) -> bool {
    !already_sent && held & (ALT | WIN) != 0
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
        Some(code) => TriggerKey::Vk(code).name().into_owned(), // VK<code>, F13..F24 by name
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
    let typing_key =
        matches!(event.event_type, EventType::KeyPress(key) if modifier_bit(key).is_none());
    let result = route_input(event);
    if result.is_none() && typing_key {
        // a swallowed key with Win or Alt held would leave Windows a lone Win/Alt tap
        let held = MODIFIERS.load(Ordering::SeqCst);
        if needs_menu_mask(held, MASK_SENT.load(Ordering::SeqCst)) {
            MASK_SENT.store(true, Ordering::SeqCst);
            #[cfg(test)]
            TEST_MASKS.fetch_add(1, Ordering::SeqCst);
            crate::injector::menu_mask();
        }
    }
    result
}

fn route_input(event: rdev::Event) -> Option<rdev::Event> {
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
        EventType::KeyPress(key) => match modifier_bit(key) {
            Some(bit) => {
                MODIFIERS.fetch_or(bit, Ordering::SeqCst);
            }
            None => resync_modifiers(),
        },
        EventType::KeyRelease(key) => {
            if let Some(bit) = modifier_bit(key) {
                MODIFIERS.fetch_and(!bit, Ordering::SeqCst);
            }
        }
        _ => {}
    }

    let held = MODIFIERS.load(Ordering::SeqCst);
    if held & (ALT | WIN) == 0 {
        MASK_SENT.store(false, Ordering::SeqCst);
    }

    // waiting for input: everything belongs to the key binding, nothing to morsing. The
    // captured key itself is not hidden. A slot key held when capture began still gets its
    // release (and a swallowed one stays swallowed), so it is not stuck as "down".
    {
        let mut capture = lock_or_recover(&CAPTURE);
        if capture.active {
            capture.feed(&event.event_type, held);
            let hide = function_keys::with_table(|t| t.handle(&event.event_type, held, false));
            return if hide { None } else { Some(event) };
        }
    }

    // the shortcut toggles Morse input, even while our own typing is suppressed. Only the
    // first press counts: holding the key sends repeated presses that would flip it back
    // and forth. The shortcut's key is not a Morse key then.
    let toggle = shortcut_pressed(&SHORTCUT, &SHORTCUT_DOWN, &event.event_type, held);
    let cycle = shortcut_pressed(&CYCLE_SHORTCUT, &CYCLE_DOWN, &event.event_type, held);
    if toggle == Some(true) {
        ACTIVE.fetch_xor(true, Ordering::SeqCst);
    }
    if cycle == Some(true) {
        CYCLE_COUNT.fetch_add(1, Ordering::SeqCst);
    }
    let undo = shortcut_pressed(&UNDO_SHORTCUT, &UNDO_DOWN, &event.event_type, held);
    if undo == Some(true) {
        UNDO_COUNT.fetch_add(1, Ordering::SeqCst);
    }
    let shortcut_used = toggle.is_some() || cycle.is_some() || undo.is_some();

    // function-key slots and the Esc abort. Own typing (`suppress_for`) can't fire a slot, so
    // a macro with `{F13}` doesn't start itself; releases are handled even then
    let suppressed = is_suppressed(now);
    if function_keys::on_event(&event.event_type, held, now, suppressed, active()) {
        return None;
    }

    if shortcut_used {
        return Some(event);
    }

    let mut guard = lock_or_recover(&ROUTE);
    let Some(route) = guard.as_mut() else {
        return Some(event);
    };
    if !suppressed && route.swallows_caps_lock(&event.event_type) {
        return None;
    }
    // While our own typing is in flight (`suppressed`) new presses of a Morse key are dropped,
    // since rdev can't tell our synthetic events from the user's. Releases of accepted presses
    // still get through, and a key that would be hidden stays hidden.
    let Route { keys, tx } = route;
    let down_count = keys.iter().filter(|k| k.down).count();
    for key in keys.iter_mut() {
        let Some(edge) = route_event(key.binding.key, &event.event_type) else {
            continue;
        };
        let other_down = down_count > usize::from(key.down);
        let Some(verdict) = key.message(edge, held, now, suppressed || other_down) else {
            continue;
        };
        if let Verdict::Send(message) = verdict {
            // an error means the worker ended, and clear_route removes the route soon
            let _ = tx.send(message);
        }
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
    *lock_or_recover(&SHORTCUT) = parse_shortcut(text)?;
    Ok(())
}

/// Sets the shortcut for the next alphabet of the list; an empty string removes it.
pub fn set_cycle_shortcut(text: &str) -> Result<(), String> {
    *lock_or_recover(&CYCLE_SHORTCUT) = parse_shortcut(text)?;
    Ok(())
}

/// How many times the cycle shortcut was pressed since the process started (wraps around).
pub fn cycle_count() -> u32 {
    CYCLE_COUNT.load(Ordering::SeqCst)
}

/// Sets the shortcut that undoes the last typed output; an empty string removes it.
pub fn set_undo_shortcut(text: &str) -> Result<(), String> {
    *lock_or_recover(&UNDO_SHORTCUT) = parse_shortcut(text)?;
    Ok(())
}

/// How many times the undo shortcut was pressed since the process started (wraps around).
pub fn undo_count() -> u32 {
    UNDO_COUNT.load(Ordering::SeqCst)
}

/// Replaces the function-key slots with `(slot, binding, swallow)` entries. Fails without
/// changing anything if an entry is invalid, duplicated or taken by the Morse keys, the
/// on/off shortcut, the cycle shortcut or the undo shortcut (those are only known while an engine runs).
pub fn set_function_keys(entries: &[(u16, String, bool)]) -> Result<(), String> {
    let mut reserved: Vec<Reserved> = Vec::new();
    if let Some(route) = lock_or_recover(&ROUTE).as_ref() {
        for key in &route.keys {
            let name = if key.symbol == Some(Symbol::Dash) {
                "der Strich-Taste"
            } else {
                "der Morse-Taste"
            };
            reserved.push((name, key.binding));
        }
    }
    if let Some(binding) = *lock_or_recover(&SHORTCUT) {
        reserved.push(("dem Ein/Aus-Kürzel", binding));
    }
    if let Some(binding) = *lock_or_recover(&CYCLE_SHORTCUT) {
        reserved.push(("dem Umschalt-Kürzel", binding));
    }
    if let Some(binding) = *lock_or_recover(&UNDO_SHORTCUT) {
        reserved.push(("dem Rückgängig-Kürzel", binding));
    }
    let parsed = function_keys::parse_entries(entries, &reserved)?;
    function_keys::with_table(|table| table.set(parsed));
    Ok(())
}

/// Removes all function-key slots.
pub fn clear_function_keys() {
    function_keys::with_table(|table| table.set(Vec::new()));
}

/// Next fired slot, oldest first.
pub fn poll_function_key() -> Option<u16> {
    function_keys::with_table(function_keys::SlotTable::pop)
}

/// True if no Ctrl, Alt, Shift or Win key is held (on Windows checked against the real
/// keyboard state, otherwise as far as the hook has seen).
pub fn keyboard_modifiers_released() -> bool {
    resync_modifiers();
    MODIFIERS.load(Ordering::SeqCst) & (CTRL | ALT | SHIFT | WIN) == 0
}

fn parse_shortcut(text: &str) -> Result<Option<KeyBinding>, String> {
    if text.trim().is_empty() {
        Ok(None)
    } else {
        text.parse::<KeyBinding>().map(Some)
    }
}

fn is_suppressed(now: Instant) -> bool {
    let guard = lock_or_recover(&SUPPRESS_UNTIL);
    guard.is_some_and(|until| now < until)
}

/// Called when the hook loop returns, which it only does when the hook is gone: an error
/// (no X11 display, missing accessibility permission on macOS) or a quit of the message
/// loop. Python reads the message via `hook_error()` and shows it. The started flag is
/// cleared so the next `install_route` / `begin_capture` starts a new hook.
fn record_hook_end<E: std::fmt::Debug>(result: Result<(), E>) {
    let message = match result {
        Err(err) => format!("Globaler Input-Hook fehlgeschlagen: {err:?}"),
        Ok(()) => "Globaler Input-Hook wurde beendet".to_string(),
    };
    *lock_or_recover(&HOOK_ERROR) = Some(message);
    HOOK_STARTED.store(false, Ordering::Release);
}

/// Starts the hook thread if none is running (once per process unless it ended).
fn ensure_hook_thread() -> io::Result<()> {
    // compare_exchange instead of OnceLock so a failed spawn can be retried
    if HOOK_STARTED
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Ok(());
    }
    *lock_or_recover(&HOOK_ERROR) = None; // a new attempt, the old error is history
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
            record_hook_end(result);
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
        let mut new = routed(keys);
        // a key held right now keeps its state, so its release is not lost or leaked to the app
        for key in &mut new {
            let same =
                |old: &&RoutedKey| old.binding.key == key.binding.key && old.symbol == key.symbol;
            if let Some(old) = route.keys.iter().find(same) {
                key.down = old.down;
                key.swallowed = old.swallowed;
                key.ignored = old.ignored;
            }
        }
        route.keys = new;
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
        assert_eq!(key_token(Key::Unknown(124)), "F13");
        assert_eq!(key_token(Key::Unknown(135)), "F24");
        assert_eq!(key_token(Key::Unknown(136)), "VK136");
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
        *lock_or_recover(&CYCLE_SHORTCUT) = None;
        CYCLE_DOWN.store(false, Ordering::SeqCst);
        *lock_or_recover(&UNDO_SHORTCUT) = None;
        UNDO_DOWN.store(false, Ordering::SeqCst);
        *lock_or_recover(&SUPPRESS_UNTIL) = None;
        function_keys::reset();
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
    fn cycle_shortcut_counts_each_press_once_and_leaves_morse_input_alone() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let start = cycle_count();
        set_cycle_shortcut("Ctrl+Alt+F9").unwrap();
        handle_event(press(Key::F9)); // no modifiers held
        assert_eq!(cycle_count(), start);
        handle_event(release(Key::F9));
        handle_event(press(Key::ControlLeft));
        handle_event(press(Key::Alt));
        for _ in 0..4 {
            handle_event(press(Key::F9)); // key repeat while held
        }
        assert_eq!(cycle_count(), start + 1, "four repeats are one press");
        handle_event(release(Key::F9));
        handle_event(press(Key::F9));
        assert_eq!(cycle_count(), start + 2, "a new press counts again");
        assert!(active(), "the cycle shortcut never switches Morse input");
        set_cycle_shortcut("").unwrap();
        handle_event(release(Key::F9));
        handle_event(press(Key::F9));
        assert_eq!(cycle_count(), start + 2, "removed shortcut does nothing");
        assert!(set_cycle_shortcut("Ctrl+Foo").is_err());
        reset_block_state();
    }

    #[test]
    fn undo_shortcut_counts_each_press_once_and_leaves_morse_input_alone() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let start = undo_count();
        let cycle_start = cycle_count();
        set_undo_shortcut("Ctrl+Alt+F8").unwrap();
        handle_event(press(Key::F8)); // no modifiers held
        assert_eq!(undo_count(), start);
        handle_event(release(Key::F8));
        handle_event(press(Key::ControlLeft));
        handle_event(press(Key::Alt));
        for _ in 0..4 {
            handle_event(press(Key::F8)); // key repeat while held
        }
        assert_eq!(undo_count(), start + 1, "four repeats are one press");
        handle_event(release(Key::F8));
        handle_event(press(Key::F8));
        assert_eq!(undo_count(), start + 2, "a new press counts again");
        assert_eq!(cycle_count(), cycle_start, "the cycle counter stays");
        assert!(active(), "the undo shortcut never switches Morse input");
        set_undo_shortcut("").unwrap();
        handle_event(release(Key::F8));
        handle_event(press(Key::F8));
        assert_eq!(undo_count(), start + 2, "removed shortcut does nothing");
        assert!(set_undo_shortcut("Ctrl+Foo").is_err());
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

    const F13: Key = Key::Unknown(124);

    fn slots(entries: &[(u16, &str, bool)]) {
        let owned: Vec<_> = entries
            .iter()
            .map(|&(i, b, s)| (i, b.to_string(), s))
            .collect();
        set_function_keys(&owned).unwrap();
    }

    fn fired_slots() -> Vec<u16> {
        std::iter::from_fn(poll_function_key).collect()
    }

    #[test]
    fn slot_press_is_queued_and_swallowed_only_when_asked() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        slots(&[(13, "F13", true), (14, "F14", false)]);
        assert!(handle_event(press(F13)).is_none(), "swallowed");
        assert!(handle_event(press(F13)).is_none(), "repeat");
        assert!(handle_event(release(F13)).is_none(), "no orphan key-up");
        assert!(
            handle_event(press(Key::Unknown(125))).is_some(),
            "F14 passes"
        );
        handle_event(release(Key::Unknown(125)));
        assert!(handle_event(press(Key::F9)).is_some(), "unassigned key");
        assert_eq!(fired_slots(), vec![13, 14]);
        assert_eq!(poll_function_key(), None);
        clear_function_keys();
        assert!(handle_event(press(F13)).is_some(), "cleared table");
        assert!(fired_slots().is_empty());
        reset_block_state();
    }

    #[test]
    fn slot_needs_its_exact_modifiers_in_the_hook() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        slots(&[(25, "Ctrl+Alt+Shift+Win+F13", true), (1, "Ctrl+F13", true)]);
        for key in [Key::ControlLeft, Key::Alt, Key::ShiftLeft, Key::MetaLeft] {
            handle_event(press(key));
        }
        assert!(handle_event(press(F13)).is_none());
        assert_eq!(
            fired_slots(),
            vec![25],
            "Ctrl+F13 does not fire under four modifiers"
        );
        handle_event(release(F13));
        for key in [Key::Alt, Key::ShiftLeft, Key::MetaLeft] {
            handle_event(release(key));
        }
        handle_event(press(F13));
        assert_eq!(fired_slots(), vec![1]);
        handle_event(release(F13));
        handle_event(release(Key::ControlLeft));
        handle_event(press(F13));
        assert!(fired_slots().is_empty(), "plain F13 is not assigned");
        clear_function_keys();
        reset_block_state();
    }

    #[test]
    fn own_typing_cannot_fire_a_slot_but_its_release_is_still_seen() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        slots(&[(13, "F13", true)]);
        suppress_for(Duration::from_millis(500));
        assert!(handle_event(press(F13)).is_some(), "sent by the injector");
        handle_event(release(F13));
        assert!(fired_slots().is_empty());
        *lock_or_recover(&SUPPRESS_UNTIL) = None;
        handle_event(press(F13));
        // the user's release falls into a later suppression window
        suppress_for(Duration::from_millis(500));
        assert!(
            handle_event(release(F13)).is_none(),
            "swallowed press, swallowed release"
        );
        *lock_or_recover(&SUPPRESS_UNTIL) = None;
        handle_event(press(F13));
        assert_eq!(fired_slots(), vec![13, 13], "not stuck as held");
        clear_function_keys();
        reset_block_state();
    }

    #[test]
    fn slots_do_nothing_while_morse_input_is_off() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        slots(&[(13, "F13", true)]);
        set_active(false);
        assert!(handle_event(press(F13)).is_some());
        handle_event(release(F13));
        assert!(fired_slots().is_empty());
        set_active(true);
        assert!(handle_event(press(F13)).is_none());
        assert_eq!(fired_slots(), vec![13]);
        clear_function_keys();
        reset_block_state();
    }

    #[test]
    fn slots_clash_with_morse_keys_and_shortcuts() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let _rx = route_keys("F11", Some("Ctrl+F12"));
        set_shortcut("Ctrl+Shift+Pause").unwrap();
        set_cycle_shortcut("Alt+F9").unwrap();
        set_undo_shortcut("Alt+F8").unwrap();
        let try_slot =
            |binding: &str| set_function_keys(&[(1, binding.to_string(), true)]).unwrap_err();
        assert!(try_slot("F11").contains("Morse-Taste"));
        assert!(try_slot("Shift+F11").contains("Morse-Taste"));
        assert!(try_slot("Ctrl+Alt+F12").contains("Strich-Taste"));
        assert!(try_slot("Ctrl+Shift+Alt+Pause").contains("Ein/Aus"));
        assert!(try_slot("Alt+F9").contains("Umschalt"));
        assert!(try_slot("Alt+F8").contains("Rückgängig"));
        assert!(
            set_function_keys(&[(1, "F12".to_string(), true)]).is_ok(),
            "needs fewer modifiers than the dash key"
        );
        assert!(set_function_keys(&[(1, "F13".to_string(), true)]).is_ok());
        // a failed call leaves the old table alone
        assert!(set_function_keys(&[(2, "F11".to_string(), true)]).is_err());
        handle_event(press(F13));
        assert_eq!(fired_slots(), vec![1]);
        clear_function_keys();
        clear_route();
        set_shortcut("").unwrap();
        reset_block_state();
    }

    #[test]
    fn modifier_wait_sees_held_keys() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        assert!(keyboard_modifiers_released());
        handle_event(press(Key::ControlRight));
        assert!(!keyboard_modifiers_released());
        handle_event(release(Key::ControlRight));
        handle_event(press(Key::CapsLock));
        assert!(keyboard_modifiers_released(), "Caps Lock is a toggle");
        handle_event(release(Key::CapsLock));
        handle_event(press(Key::Unknown(92)));
        assert!(!keyboard_modifiers_released(), "right Windows key");
        handle_event(release(Key::Unknown(92)));
        assert!(keyboard_modifiers_released());
        reset_block_state();
    }

    #[test]
    fn one_esc_sets_the_abort_flag_and_is_never_swallowed() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        assert!(!function_keys::abort_requested());
        assert!(handle_event(press(Key::Escape)).is_some());
        handle_event(release(Key::Escape));
        assert!(function_keys::abort_requested(), "peek does not clear");
        assert!(function_keys::take_abort());
        assert!(!function_keys::take_abort(), "reading clears it");
        // key repeat of a held Esc is one press
        handle_event(press(Key::Escape));
        assert!(function_keys::take_abort());
        handle_event(press(Key::Escape));
        assert!(!function_keys::take_abort());
        handle_event(release(Key::Escape));
        reset_block_state();
    }

    #[test]
    fn esc_while_we_type_aborts_but_the_esc_we_send_does_not() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        // our typing keeps the guard up; the user's Esc must still count
        suppress_for(Duration::from_millis(500));
        assert!(handle_event(press(Key::Escape)).is_some());
        handle_event(release(Key::Escape));
        assert!(function_keys::take_abort());
        // an {Esc} from the injector is announced first
        function_keys::ignore_esc_for(Duration::from_millis(500));
        handle_event(press(Key::Escape));
        handle_event(release(Key::Escape));
        assert!(!function_keys::take_abort());
        reset_block_state();
    }

    /// Short names of what the worker received: P(ress), R(elease), D(ot) / S(trich) release.
    fn kinds(rx: &std::sync::mpsc::Receiver<WorkerMessage>) -> String {
        rx.try_iter()
            .map(|m| match m {
                WorkerMessage::Press(_) => 'P',
                WorkerMessage::Release(_) => 'R',
                WorkerMessage::ReleaseAs(_, Symbol::Dot) => 'D',
                WorkerMessage::ReleaseAs(_, Symbol::Dash) => 'S',
                _ => '?',
            })
            .collect()
    }

    #[test]
    fn hook_end_is_recorded_and_allows_a_restart() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        for result in [Ok(()), Err("kein Display")] {
            HOOK_STARTED.store(true, Ordering::SeqCst);
            *lock_or_recover(&HOOK_ERROR) = None;
            record_hook_end(result);
            let message = hook_error().expect("every end is an error");
            assert!(message.contains("Input-Hook"), "{message}");
            assert!(!HOOK_STARTED.load(Ordering::SeqCst), "can be started again");
        }
        assert!(hook_error().unwrap().contains("kein Display"));
        *lock_or_recover(&HOOK_ERROR) = None;
    }

    #[test]
    fn modifier_sync_follows_the_real_keyboard() {
        let (ctrl_l, shift_l, win_r) = (1, 16, 128);
        // Win+L left Win and Ctrl "held" in our bits; the keyboard says only Shift is down
        let held = ctrl_l | win_r | CAPS_LOCK;
        let synced = sync_modifiers(held, |vk| vk == 0xA0);
        assert_eq!(synced, shift_l | CAPS_LOCK, "Caps Lock is not touched");
        assert_eq!(sync_modifiers(0, |vk| vk == 0x5C), win_r, "also adds");
        assert_eq!(sync_modifiers(held, |_| true), 0xFF | CAPS_LOCK);
    }

    #[test]
    fn menu_mask_is_needed_once_per_alt_or_win_hold() {
        assert!(needs_menu_mask(ALT, false));
        assert!(needs_menu_mask(WIN | SHIFT, false));
        assert!(!needs_menu_mask(ALT, true), "already sent");
        assert!(!needs_menu_mask(CTRL | SHIFT, false), "no Start/menu risk");
        assert!(!needs_menu_mask(0, false));
    }

    #[test]
    fn swallowed_slot_key_under_win_or_alt_sends_one_mask() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        MASK_SENT.store(false, Ordering::SeqCst);
        slots(&[(13, "Win+F13", true), (14, "F14", false)]);
        let before = TEST_MASKS.load(Ordering::SeqCst);
        handle_event(press(Key::MetaLeft));
        assert!(handle_event(press(F13)).is_none());
        handle_event(press(F13)); // key repeat
        handle_event(release(F13));
        assert!(handle_event(press(F13)).is_none());
        handle_event(release(F13));
        assert_eq!(TEST_MASKS.load(Ordering::SeqCst), before + 1);
        handle_event(release(Key::MetaLeft));
        // a key that is not swallowed needs none, and neither does a hold without Win/Alt
        handle_event(press(Key::Unknown(125)));
        handle_event(release(Key::Unknown(125)));
        assert_eq!(TEST_MASKS.load(Ordering::SeqCst), before + 1);
        handle_event(press(Key::Alt));
        slots(&[(13, "Alt+F13", true)]);
        handle_event(press(F13));
        assert_eq!(TEST_MASKS.load(Ordering::SeqCst), before + 2, "new hold");
        handle_event(release(F13));
        handle_event(release(Key::Alt));
        clear_function_keys();
        reset_block_state();
    }

    #[test]
    fn rolled_presses_of_two_keys_give_the_symbol_of_the_first() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let rx = route_keys("F11", Some("F12"));
        handle_event(press(Key::F11)); // dot down
        handle_event(press(Key::F12)); // dash rolled in: refused
        handle_event(release(Key::F11)); // dot
        handle_event(press(Key::F12)); // key repeat of the refused press: still refused
        handle_event(press(Key::F11)); // dot again
        handle_event(release(Key::F12)); // refused press ends, no symbol
        handle_event(release(Key::F11)); // second dot
        clear_route();
        assert_eq!(kinds(&rx), "PDPD");
        reset_block_state();
    }

    #[test]
    fn refused_rolled_key_is_still_hidden_when_blocking() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let _rx = route_keys("F11", Some("F12"));
        set_blocking(true);
        assert!(handle_event(press(Key::F11)).is_none());
        assert!(handle_event(press(Key::F12)).is_none());
        assert!(handle_event(release(Key::F11)).is_none());
        assert!(
            handle_event(release(Key::F12)).is_none(),
            "no orphan key-up"
        );
        clear_route();
        reset_block_state();
    }

    #[test]
    fn dash_key_with_modifier_is_not_shadowed_by_a_plain_dot_key() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let rx = route_keys("F8", Some("Ctrl+F8"));
        handle_event(press(Key::ControlLeft));
        handle_event(press(Key::F8));
        handle_event(release(Key::F8));
        handle_event(release(Key::ControlLeft));
        handle_event(press(Key::F8));
        handle_event(release(Key::F8));
        clear_route();
        assert_eq!(kinds(&rx), "PSPD");
        // the same with the dot key given as the specific one
        let rx = route_keys("Ctrl+F8", Some("F8"));
        handle_event(press(Key::ControlLeft));
        handle_event(press(Key::F8));
        handle_event(release(Key::F8));
        handle_event(release(Key::ControlLeft));
        clear_route();
        assert_eq!(kinds(&rx), "PD");
        reset_block_state();
    }

    #[test]
    fn shortcut_key_is_not_also_a_morse_key() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let rx = route_keys("F11", Some("Pause"));
        set_shortcut("Ctrl+Shift+Pause").unwrap();
        handle_event(press(Key::ControlLeft));
        handle_event(press(Key::ShiftLeft));
        handle_event(press(Key::Pause));
        handle_event(press(Key::Pause)); // key repeat
        handle_event(release(Key::Pause));
        assert!(!active(), "the shortcut fired");
        assert_eq!(kinds(&rx), "", "no stray dash");
        handle_event(release(Key::ShiftLeft));
        handle_event(release(Key::ControlLeft));
        // without the modifiers it is the dash key again
        handle_event(press(Key::Pause));
        handle_event(release(Key::Pause));
        assert_eq!(kinds(&rx), "PS");
        set_shortcut("").unwrap();
        clear_route();
        reset_block_state();
    }

    #[test]
    fn release_of_an_accepted_press_survives_own_typing() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let rx = route_keys("F12", None);
        handle_event(press(Key::F12));
        suppress_for(Duration::from_millis(500));
        handle_event(release(Key::F12)); // falls into the guard window of a typed character
        assert_eq!(kinds(&rx), "PR", "press is not left open");
        // a new press inside the window is dropped (it may be our own input) ...
        handle_event(press(Key::F12));
        assert_eq!(kinds(&rx), "");
        handle_event(release(Key::F12));
        assert_eq!(kinds(&rx), "");
        // ... and works again afterwards, nothing stayed "down"
        *lock_or_recover(&SUPPRESS_UNTIL) = None;
        handle_event(press(Key::F12));
        handle_event(release(Key::F12));
        assert_eq!(kinds(&rx), "PR");
        clear_route();
        reset_block_state();
    }

    #[test]
    fn blocked_key_stays_hidden_while_own_typing_runs() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let rx = route_keys("F12", None);
        set_blocking(true);
        suppress_for(Duration::from_millis(500));
        assert!(
            handle_event(press(Key::F12)).is_none(),
            "user press, hidden"
        );
        *lock_or_recover(&SUPPRESS_UNTIL) = None;
        assert!(
            handle_event(release(Key::F12)).is_none(),
            "no orphan key-up"
        );
        assert_eq!(kinds(&rx), "", "but not counted as Morse input");
        // a press accepted before the window keeps its swallowed release in it
        handle_event(press(Key::F12));
        suppress_for(Duration::from_millis(500));
        assert!(handle_event(release(Key::F12)).is_none());
        assert_eq!(kinds(&rx), "PR");
        clear_route();
        reset_block_state();
    }

    #[test]
    fn slot_key_released_during_capture_is_not_left_down() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        slots(&[(13, "F13", true)]);
        handle_event(press(F13));
        begin_test_capture();
        assert!(
            handle_event(release(F13)).is_none(),
            "swallowed press, swallowed release"
        );
        assert_eq!(take_capture(), None, "a release is not a capture");
        cancel_capture();
        handle_event(press(F13)); // a new press, not a repeat
        assert_eq!(fired_slots(), vec![13, 13]);
        handle_event(release(F13));
        clear_function_keys();
        reset_block_state();
    }

    #[test]
    fn replacing_the_slots_while_a_key_is_held_keeps_its_release_hidden() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        slots(&[(13, "F13", true)]);
        assert!(handle_event(press(F13)).is_none());
        slots(&[(14, "F14", true)]); // F13 is gone from the table
        assert!(
            handle_event(press(F13)).is_none(),
            "repeat of the old press"
        );
        assert!(handle_event(release(F13)).is_none(), "no orphan key-up");
        assert!(handle_event(press(F13)).is_some(), "the hold is over");
        assert!(fired_slots().is_empty());
        handle_event(release(F13));
        clear_function_keys();
        reset_block_state();
    }

    #[test]
    fn changing_morse_keys_while_held_keeps_the_press_state() {
        let _serial = lock_or_recover(&GLOBAL_STATE);
        reset_block_state();
        let rx = route_keys("F12", None);
        set_blocking(true);
        assert!(handle_event(press(Key::F12)).is_none());
        set_keys(MorseKeys {
            key: "F12".parse().unwrap(),
            dash_key: None,
        });
        assert!(
            handle_event(release(Key::F12)).is_none(),
            "no orphan key-up"
        );
        assert_eq!(kinds(&rx), "PR", "release still ends the press");
        clear_route();
        reset_block_state();
    }
}
