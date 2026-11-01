//! Function-key slots and the Esc-Esc abort, both fed by the hook callback.
//!
//! A slot is a key binding the user (or a Stream Deck) presses to run a macro. The hook
//! only records WHICH slot fired in a small queue; Python polls it and runs the macro.
//! Both state machines here take their inputs (event, held modifiers, timestamp) as
//! arguments, so tests need no hook and no clock.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use rdev::{EventType, Key};

use crate::hooks::{route_event, Edge};
use crate::lock_or_recover;
use crate::trigger::{KeyBinding, ModifierMask, TriggerKey, ALT, CTRL, SHIFT, WIN};

/// Highest slot number (F1..F100).
pub const MAX_SLOT: u16 = 100;
/// Fired slots waiting for Python; the oldest is dropped when it is full.
pub const QUEUE_LIMIT: usize = 64;
/// Two Esc presses within this time abort a running macro.
pub const ABORT_WINDOW: Duration = Duration::from_millis(500);

struct Slot {
    id: u16,
    binding: KeyBinding,
    /// The user wants the key hidden from the focused app.
    swallow: bool,
    /// The press was accepted, so repeats are ignored and the release counts.
    down: bool,
    /// The accepted press was swallowed, so its repeats and release are swallowed too.
    swallowed: bool,
}

/// All slots plus the queue of fired ones.
#[derive(Default)]
pub struct SlotTable {
    slots: Vec<Slot>,
    queue: VecDeque<u16>,
}

impl SlotTable {
    pub const fn new() -> Self {
        Self {
            slots: Vec::new(),
            queue: VecDeque::new(),
        }
    }

    /// Replaces the whole table; fired slots still in the queue are dropped.
    pub fn set(&mut self, entries: Vec<(u16, KeyBinding, bool)>) {
        self.slots = entries
            .into_iter()
            .map(|(id, binding, swallow)| Slot {
                id,
                binding,
                swallow,
                down: false,
                swallowed: false,
            })
            .collect();
        self.queue.clear();
    }

    /// Feeds one input event. Returns true if the event must be hidden from other apps.
    ///
    /// `may_fire` is false while our own typing is suppressed or Morse input is off:
    /// presses are then ignored, but a release still ends a press accepted earlier.
    pub fn handle(&mut self, event: &EventType, held: ModifierMask, may_fire: bool) -> bool {
        let Self { slots, queue } = self;
        let mut swallow = false;
        for slot in slots.iter_mut() {
            let Some(edge) = route_event(slot.binding.key, event) else {
                continue;
            };
            match edge {
                Edge::Press if slot.down => swallow |= slot.swallowed, // key repeat
                Edge::Press => {
                    if !may_fire || !slot.binding.modifiers_exact(held) {
                        continue;
                    }
                    slot.down = true;
                    // the left mouse button is never hidden, the app couldn't be used then
                    slot.swallowed = slot.swallow && slot.binding.key != TriggerKey::MouseLeft;
                    swallow |= slot.swallowed;
                    if queue.len() == QUEUE_LIMIT {
                        queue.pop_front();
                    }
                    queue.push_back(slot.id);
                }
                Edge::Release => {
                    if std::mem::take(&mut slot.down) {
                        swallow |= std::mem::take(&mut slot.swallowed);
                    }
                }
            }
        }
        swallow
    }

    /// Next fired slot, oldest first.
    pub fn pop(&mut self) -> Option<u16> {
        self.queue.pop_front()
    }
}

/// Recognizes two Esc presses within [`ABORT_WINDOW`].
#[derive(Default)]
pub struct AbortDetector {
    last_press: Option<Instant>,
    down: bool,
}

impl AbortDetector {
    pub const fn new() -> Self {
        Self {
            last_press: None,
            down: false,
        }
    }

    /// True on the second Esc press of a pair. Key repeat of a held Esc counts as one press,
    /// and a third quick press starts a new pair.
    pub fn press(&mut self, now: Instant) -> bool {
        if std::mem::replace(&mut self.down, true) {
            return false;
        }
        match self.last_press {
            Some(first) if now.saturating_duration_since(first) <= ABORT_WINDOW => {
                self.last_press = None;
                true
            }
            _ => {
                self.last_press = Some(now);
                false
            }
        }
    }

    pub fn release(&mut self) {
        self.down = false;
    }
}

/// Binding that is already taken by something else, with the name shown in errors.
pub type Reserved = (&'static str, KeyBinding);

/// Virtual-key codes of Shift, Ctrl, Alt, Caps Lock, both Windows keys and the left/right variants.
const MODIFIER_VKS: [u16; 12] = [16, 17, 18, 20, 91, 92, 160, 161, 162, 163, 164, 165];

/// Why a binding must not be a slot trigger, `None` if it is fine. The hook runs for every key of
/// the user, so a slot on Esc, on a modifier or on a plain typing key (with `swallow`, or not:
/// the macro would fire on every letter) would get in the way of normal use. The Python side
/// (`_unusable_trigger` in function_keys.py) has the same rules.
fn unusable_reason(binding: &KeyBinding, swallow: bool) -> Option<&'static str> {
    let shortcut = binding.requires(CTRL) || binding.requires(ALT) || binding.requires(WIN);
    match binding.key {
        TriggerKey::ControlRight | TriggerKey::AltGr => Some("die Modifikatortasten"),
        TriggerKey::Vk(27) => Some("Esc und das Abbrechen mit Esc, Esc"),
        TriggerKey::Vk(code) if MODIFIER_VKS.contains(&code) => Some("die Modifikatortasten"),
        TriggerKey::Space => (!shortcut).then_some("normales Tippen"),
        // Backspace, Tab, Enter, digits, letters, numpad and punctuation
        TriggerKey::Vk(8 | 9 | 13 | 48..=57 | 65..=90 | 96..=111 | 186..=192 | 219..=222 | 226) => {
            (!shortcut).then_some("normales Tippen")
        }
        TriggerKey::MouseRight | TriggerKey::MouseMiddle => {
            (swallow && !shortcut && !binding.requires(SHIFT)).then_some("normales Klicken")
        }
        _ => None,
    }
}

/// Parses `(slot, binding, swallow)` entries and rejects bad numbers, bad bindings,
/// duplicates and clashes with `reserved` (Morse keys and shortcuts).
pub fn parse_entries(
    entries: &[(u16, String, bool)],
    reserved: &[Reserved],
) -> Result<Vec<(u16, KeyBinding, bool)>, String> {
    let mut parsed: Vec<(u16, KeyBinding, bool)> = Vec::with_capacity(entries.len());
    for (id, text, swallow) in entries {
        if !(1..=MAX_SLOT).contains(id) {
            return Err(format!("Slot {id} liegt nicht zwischen 1 und {MAX_SLOT}"));
        }
        if parsed.iter().any(|(other, ..)| other == id) {
            return Err(format!("Slot F{id} ist doppelt angegeben"));
        }
        let binding = text
            .parse::<KeyBinding>()
            .map_err(|e| format!("F{id}: {e}"))?;
        if let Some(why) = unusable_reason(&binding, *swallow) {
            return Err(format!(
                "F{id}: {binding} ist als Auslöser nicht erlaubt (stört {why})"
            ));
        }
        for (other, taken, _) in &parsed {
            if *taken == binding {
                return Err(format!(
                    "F{other} und F{id} haben denselben Auslöser ({binding})"
                ));
            }
        }
        for (name, taken) in reserved {
            if taken.fires_with(&binding) {
                return Err(format!("F{id} ({binding}) kollidiert mit {name} ({taken})"));
            }
        }
        parsed.push((*id, binding, *swallow));
    }
    Ok(parsed)
}

static TABLE: Mutex<SlotTable> = Mutex::new(SlotTable::new());
static ESC: Mutex<AbortDetector> = Mutex::new(AbortDetector::new());
static ABORT: AtomicBool = AtomicBool::new(false);

pub fn with_table<R>(f: impl FnOnce(&mut SlotTable) -> R) -> R {
    f(&mut lock_or_recover(&TABLE))
}

/// Hook entry point: slot routing and Esc-Esc detection for one input event.
/// Returns true if the event must be hidden from other apps.
///
/// `suppressed` is true while our own typing is in flight (see `hooks::suppress_for`):
/// those events can't fire a slot or count as Esc. `active` is false while Morse input
/// is switched off; slots do nothing then.
pub fn on_event(
    event: &EventType,
    held: ModifierMask,
    now: Instant,
    suppressed: bool,
    active: bool,
) -> bool {
    match event {
        EventType::KeyPress(Key::Escape) if !suppressed => {
            if lock_or_recover(&ESC).press(now) {
                ABORT.store(true, Ordering::SeqCst);
            }
        }
        EventType::KeyRelease(Key::Escape) => lock_or_recover(&ESC).release(),
        _ => {}
    }
    with_table(|table| table.handle(event, held, !suppressed && active))
}

/// Reads and clears the abort flag.
pub fn take_abort() -> bool {
    ABORT.swap(false, Ordering::SeqCst)
}

/// Test helper: back to the initial state.
#[cfg(test)]
pub fn reset() {
    *lock_or_recover(&TABLE) = SlotTable::new();
    *lock_or_recover(&ESC) = AbortDetector::new();
    ABORT.store(false, Ordering::SeqCst);
}

#[cfg(test)]
mod tests {
    use super::*;
    use rdev::Button;

    const CTRL_L: ModifierMask = 0b01;
    const ALT_L: ModifierMask = 0b100;
    const SHIFT_L: ModifierMask = 0b1_0000;
    const WIN_L: ModifierMask = 0b100_0000;

    fn f(n: u32) -> EventType {
        EventType::KeyPress(Key::Unknown(124 + n - 13))
    }

    fn f_up(n: u32) -> EventType {
        EventType::KeyRelease(Key::Unknown(124 + n - 13))
    }

    fn table(entries: &[(u16, &str, bool)]) -> SlotTable {
        let owned: Vec<_> = entries.iter().map(|&(i, b, s)| (i, b.into(), s)).collect();
        let mut table = SlotTable::new();
        table.set(parse_entries(&owned, &[]).unwrap());
        table
    }

    fn fired(table: &mut SlotTable) -> Vec<u16> {
        std::iter::from_fn(|| table.pop()).collect()
    }

    #[test]
    fn a_press_queues_its_slot_once_and_ignores_key_repeat() {
        let mut t = table(&[(13, "F13", true)]);
        t.handle(&f(13), 0, true);
        for _ in 0..5 {
            t.handle(&f(13), 0, true);
        }
        assert_eq!(fired(&mut t), vec![13]);
        t.handle(&f_up(13), 0, true);
        t.handle(&f(13), 0, true);
        assert_eq!(fired(&mut t), vec![13], "a new press fires again");
    }

    #[test]
    fn modifiers_must_match_exactly() {
        let mut t = table(&[(13, "F13", false), (14, "Ctrl+F13", false)]);
        t.handle(&f(13), 0, true);
        t.handle(&f_up(13), 0, true);
        t.handle(&f(13), CTRL_L, true);
        t.handle(&f_up(13), CTRL_L, true);
        t.handle(&f(13), CTRL_L | SHIFT_L, true);
        assert_eq!(fired(&mut t), vec![13, 14], "Ctrl+Shift+F13 is neither");
    }

    #[test]
    fn four_modifier_combo_does_not_trigger_the_shorter_ones() {
        let mut t = table(&[
            (25, "Ctrl+Alt+Shift+Win+F13", true),
            (26, "Ctrl+Alt+Shift+F13", true),
        ]);
        let all = CTRL_L | ALT_L | SHIFT_L | WIN_L;
        assert!(t.handle(&f(13), all, true));
        assert_eq!(fired(&mut t), vec![25]);
        t.handle(&f_up(13), all, true);
        t.handle(&f(13), CTRL_L | ALT_L | SHIFT_L, true);
        assert_eq!(fired(&mut t), vec![26]);
    }

    #[test]
    fn swallow_hides_press_repeat_and_release_only_for_accepted_presses() {
        let mut t = table(&[(13, "F13", true), (14, "F14", false)]);
        assert!(t.handle(&f(13), 0, true));
        assert!(t.handle(&f(13), 0, true), "repeat");
        assert!(t.handle(&f_up(13), 0, true), "no orphan key-up for the app");
        assert!(!t.handle(&f(14), 0, true), "pass-through slot");
        assert!(!t.handle(&f_up(14), 0, true));
        assert!(!t.handle(&f(15), 0, true), "unassigned keys always pass");
        assert!(!t.handle(&f_up(15), 0, true));
    }

    #[test]
    fn a_rejected_press_is_passed_on_with_its_release() {
        let mut t = table(&[(13, "Ctrl+F13", true)]);
        assert!(!t.handle(&f(13), 0, true), "Ctrl missing: normal F13");
        assert!(!t.handle(&f_up(13), 0, true));
        assert!(fired(&mut t).is_empty());
    }

    #[test]
    fn suppressed_or_inactive_presses_do_nothing_but_releases_still_count() {
        let mut t = table(&[(13, "F13", true)]);
        assert!(!t.handle(&f(13), 0, false), "our own typing");
        assert!(fired(&mut t).is_empty());
        t.handle(&f(13), 0, true);
        // the release arrives while our own typing is suppressed
        assert!(t.handle(&f_up(13), 0, false));
        t.handle(&f(13), 0, true);
        assert_eq!(fired(&mut t), vec![13, 13], "not stuck as 'held'");
    }

    #[test]
    fn mouse_slots_work_and_the_left_button_is_never_swallowed() {
        let mut t = table(&[(1, "Mouse5", true), (2, "MouseLeft", true)]);
        let button5 = EventType::ButtonPress(Button::Unknown(2));
        assert!(t.handle(&button5, 0, true));
        assert!(!t.handle(&EventType::ButtonPress(Button::Left), 0, true));
        assert_eq!(fired(&mut t), vec![1, 2]);
    }

    #[test]
    fn queue_keeps_the_newest_entries() {
        let mut t = table(&[(13, "F13", false)]);
        for _ in 0..QUEUE_LIMIT + 10 {
            t.handle(&f(13), 0, true);
            t.handle(&f_up(13), 0, true);
        }
        assert_eq!(fired(&mut t).len(), QUEUE_LIMIT);
        let mut t = table(&[(1, "F13", false), (2, "F14", false)]);
        t.handle(&f(13), 0, true);
        for _ in 0..QUEUE_LIMIT {
            t.handle(&f(14), 0, true);
            t.handle(&f_up(14), 0, true);
        }
        assert_eq!(fired(&mut t).first(), Some(&2), "the oldest was dropped");
    }

    #[test]
    fn setting_a_new_table_drops_queue_and_held_state() {
        let mut t = table(&[(13, "F13", true)]);
        t.handle(&f(13), 0, true);
        t.set(vec![("F13".parse().map(|b| (13, b, true))).unwrap()]);
        assert!(fired(&mut t).is_empty());
        assert!(t.handle(&f(13), 0, true), "a fresh press, not a repeat");
    }

    fn entry(slot: u16, binding: &str) -> (u16, String, bool) {
        (slot, binding.to_string(), true)
    }

    #[test]
    fn entries_are_checked() {
        let ok = parse_entries(&[entry(1, "f13"), entry(100, "Strg+Alt+F24")], &[]).unwrap();
        assert_eq!(ok.len(), 2);
        assert_eq!(ok[1].1.to_string(), "Ctrl+Alt+F24");
        for (bad, why) in [
            (vec![entry(0, "F13")], "Slot 0"),
            (vec![entry(101, "F13")], "Slot 101"),
            (vec![entry(1, "Hyper+F13")], "F1: "),
            (vec![entry(1, "")], "F1: "),
            (vec![entry(1, "F13"), entry(1, "F14")], "doppelt"),
            (
                vec![entry(1, "F13"), entry(2, "ctrl+f13"), entry(3, "F13")],
                "denselben",
            ),
            (
                vec![entry(5, "Ctrl+Shift+F13"), entry(6, "shift+Strg+VK124")],
                "denselben",
            ),
        ] {
            let err = parse_entries(&bad, &[]).unwrap_err();
            assert!(err.contains(why), "{err}");
        }
    }

    #[test]
    fn triggers_that_would_break_normal_use_are_refused() {
        for bad in [
            "VK27",
            "Ctrl+VK27",
            "ControlRight",
            "Ctrl+AltGr",
            "VK16",
            "VK91",
            "VK20",
            "VK65",
            "Shift+VK65",
            "VK13",
            "VK8",
            "Space",
            "Shift+Space",
            "VK48",
            "VK100",
        ] {
            let err = parse_entries(&[entry(1, bad)], &[]).unwrap_err();
            assert!(err.contains("nicht erlaubt"), "{bad}: {err}");
        }
        let err = parse_entries(&[entry(1, "MouseRight")], &[]).unwrap_err();
        assert!(err.contains("Klicken"), "{err}");
        for ok in [
            "Ctrl+VK65",
            "Alt+Space",
            "Win+VK13",
            "F13",
            "F5",
            "Shift+F13",
            "Insert",
            "Pause",
            "MouseLeft",
            "Mouse5",
            "Shift+MouseRight",
            "Ctrl+MouseMiddle",
            "VK112",
        ] {
            assert!(parse_entries(&[entry(1, ok)], &[]).is_ok(), "{ok}");
        }
        let pass_through = [(1, "MouseRight".to_string(), false)];
        assert!(
            parse_entries(&pass_through, &[]).is_ok(),
            "not hidden, so right-click still works"
        );
    }

    #[test]
    fn entries_clash_with_morse_keys_and_shortcuts() {
        let b = |s: &str| s.parse::<KeyBinding>().unwrap();
        let reserved = [
            ("Morse-Taste", b("F13")),
            ("Ein/Aus-Kürzel", b("Ctrl+Pause")),
        ];
        let err = parse_entries(&[entry(7, "Ctrl+F13")], &reserved).unwrap_err();
        assert!(err.contains("F7") && err.contains("Morse-Taste"), "{err}");
        let err = parse_entries(&[entry(8, "Ctrl+Pause")], &reserved).unwrap_err();
        assert!(err.contains("Ein/Aus-Kürzel"), "{err}");
        // a Morse key that needs MORE modifiers than the slot is not triggered by it
        let reserved = [("Morse-Taste", b("Ctrl+F13"))];
        assert!(parse_entries(&[entry(7, "F13")], &reserved).is_ok());
    }

    #[test]
    fn esc_twice_within_the_window_aborts() {
        let t0 = Instant::now();
        let mut esc = AbortDetector::new();
        assert!(!esc.press(t0));
        esc.release();
        assert!(esc.press(t0 + Duration::from_millis(300)));
        esc.release();
        assert!(
            !esc.press(t0 + Duration::from_millis(400)),
            "pair was used up"
        );
    }

    #[test]
    fn esc_outside_the_window_or_held_does_not_abort() {
        let t0 = Instant::now();
        let mut esc = AbortDetector::new();
        esc.press(t0);
        esc.release();
        assert!(!esc.press(t0 + Duration::from_millis(501)));
        esc.release();
        // exactly on the limit still counts
        assert!(esc.press(t0 + Duration::from_millis(501) + ABORT_WINDOW));
        esc.release();
        // key repeat of a held Esc is one press
        let mut esc = AbortDetector::new();
        assert!(!esc.press(t0));
        assert!(!esc.press(t0 + Duration::from_millis(30)));
        assert!(!esc.press(t0 + Duration::from_millis(60)));
    }
}
