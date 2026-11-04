//! Which key or mouse button acts as the Morse key.
//!
//! A dedicated enum instead of `rdev::Key` keeps configuration, parsing and tests
//! independent of `rdev` (the `native-io` feature is optional).

use std::borrow::Cow;
use std::fmt;
use std::str::FromStr;

/// Virtual-key codes of F13 and F24 (VK_F13..VK_F24 are consecutive).
const F13_VK: u16 = 124;
const F24_VK: u16 = 135;

/// Allowed triggers. The names (see [`TriggerKey::name`]) are the stable interface to
/// the Python configuration.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum TriggerKey {
    // keyboard: keys that are rarely used day to day
    /// Default. The hooks can't swallow key presses (rdev::listen only observes), and
    /// almost no app does anything with Scroll Lock, so letting it through is harmless.
    #[default]
    ScrollLock,
    Pause,
    Insert,
    ControlRight,
    AltGr,
    F1,
    F2,
    F3,
    F4,
    F5,
    F6,
    F7,
    F8,
    F9,
    F10,
    F11,
    F12,
    /// Space bar. Every press also types a space in the active app.
    Space,
    // mouse
    MouseLeft,
    MouseRight,
    MouseMiddle,
    // any other input (not in `ALL`, but valid)
    /// Any keyboard key by its Windows virtual-key code (1..=255), named `VK<code>`,
    /// e.g. `VK65` for A. F13..F24 (codes 124..=135) are named `F13`..`F24` instead;
    /// `VK124` still parses and means the same key.
    Vk(u16),
    /// Extra mouse button from Mouse4 on (`Mouse4`, `Mouse5`, ...).
    MouseButton(u8),
}

impl TriggerKey {
    /// All triggers in UI order.
    pub const ALL: &'static [TriggerKey] = &[
        TriggerKey::ScrollLock,
        TriggerKey::Pause,
        TriggerKey::Insert,
        TriggerKey::ControlRight,
        TriggerKey::AltGr,
        TriggerKey::F1,
        TriggerKey::F2,
        TriggerKey::F3,
        TriggerKey::F4,
        TriggerKey::F5,
        TriggerKey::F6,
        TriggerKey::F7,
        TriggerKey::F8,
        TriggerKey::F9,
        TriggerKey::F10,
        TriggerKey::F11,
        TriggerKey::F12,
        TriggerKey::Space,
        TriggerKey::MouseLeft,
        TriggerKey::MouseRight,
        TriggerKey::MouseMiddle,
    ];

    /// Stable ASCII name for config files.
    pub fn name(self) -> Cow<'static, str> {
        match self {
            TriggerKey::Vk(code @ F13_VK..=F24_VK) => {
                Cow::Owned(format!("F{}", code - F13_VK + 13))
            }
            TriggerKey::Vk(code) => Cow::Owned(format!("VK{code}")),
            TriggerKey::MouseButton(n) => Cow::Owned(format!("Mouse{n}")),
            other => Cow::Borrowed(other.legacy_name()),
        }
    }

    fn legacy_name(self) -> &'static str {
        match self {
            TriggerKey::ScrollLock => "ScrollLock",
            TriggerKey::Pause => "Pause",
            TriggerKey::Insert => "Insert",
            TriggerKey::ControlRight => "ControlRight",
            TriggerKey::AltGr => "AltGr",
            TriggerKey::F1 => "F1",
            TriggerKey::F2 => "F2",
            TriggerKey::F3 => "F3",
            TriggerKey::F4 => "F4",
            TriggerKey::F5 => "F5",
            TriggerKey::F6 => "F6",
            TriggerKey::F7 => "F7",
            TriggerKey::F8 => "F8",
            TriggerKey::F9 => "F9",
            TriggerKey::F10 => "F10",
            TriggerKey::F11 => "F11",
            TriggerKey::F12 => "F12",
            TriggerKey::Space => "Space",
            TriggerKey::MouseLeft => "MouseLeft",
            TriggerKey::MouseRight => "MouseRight",
            TriggerKey::MouseMiddle => "MouseMiddle",
            TriggerKey::Vk(_) | TriggerKey::MouseButton(_) => unreachable!("dynamische Namen"),
        }
    }

    pub fn is_mouse(self) -> bool {
        matches!(
            self,
            TriggerKey::MouseLeft
                | TriggerKey::MouseRight
                | TriggerKey::MouseMiddle
                | TriggerKey::MouseButton(_)
        )
    }
}

impl fmt::Display for TriggerKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.name())
    }
}

/// Error when parsing a trigger name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownTrigger(pub String);

impl fmt::Display for UnknownTrigger {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let names: Vec<Cow<'static, str>> = TriggerKey::ALL.iter().map(|t| t.name()).collect();
        write!(
            f,
            "Unbekannter Taster '{}'. Erlaubt: {}",
            self.0,
            names.join(", ")
        )
    }
}

impl std::error::Error for UnknownTrigger {}

impl FromStr for TriggerKey {
    type Err = UnknownTrigger;

    /// Case and surrounding whitespace are ignored, so a hand-written config doesn't
    /// fail on "f8".
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let wanted = s.trim();
        if let Some(found) = TriggerKey::ALL
            .iter()
            .copied()
            .find(|t| t.name().eq_ignore_ascii_case(wanted))
        {
            return Ok(found);
        }
        let lower = wanted.to_ascii_lowercase();
        if let Some(n) = lower.strip_prefix('f').and_then(|n| n.parse::<u16>().ok()) {
            if (13..=24).contains(&n) && lower.len() == 3 {
                return Ok(TriggerKey::Vk(F13_VK + n - 13));
            }
        }
        if let Some(code) = lower.strip_prefix("vk").and_then(|n| n.parse::<u16>().ok()) {
            if (1..=255).contains(&code) {
                return Ok(TriggerKey::Vk(code));
            }
        }
        if let Some(n) = lower
            .strip_prefix("mouse")
            .and_then(|n| n.parse::<u8>().ok())
        {
            if (4..=32).contains(&n) {
                return Ok(TriggerKey::MouseButton(n));
            }
        }
        Err(UnknownTrigger(s.to_string()))
    }
}

/// Bit mask of held modifier keys, one bit per physical key (left/right count separately,
/// see `hooks::modifier_bit`). The group masks below cover both sides of a modifier.
pub type ModifierMask = u16;

pub const CTRL: ModifierMask = 0b0_0000_0011;
pub const ALT: ModifierMask = 0b0_0000_1100;
pub const SHIFT: ModifierMask = 0b0_0011_0000;
pub const WIN: ModifierMask = 0b0_1100_0000;
pub const CAPS_LOCK: ModifierMask = 0b1_0000_0000;

/// Modifier groups in display order with their canonical names.
const MODIFIER_GROUPS: [(ModifierMask, &str); 5] = [
    (CTRL, "Ctrl"),
    (ALT, "Alt"),
    (SHIFT, "Shift"),
    (WIN, "Win"),
    (CAPS_LOCK, "CapsLock"),
];

/// A key that only counts while certain modifiers are held, e.g. `CapsLock+VK81` (Caps
/// Lock + Q) as a Morse key or `Ctrl+Shift+Pause` as the on/off shortcut. Without
/// modifiers it is a plain key such as `ScrollLock`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KeyBinding {
    /// Group masks of the modifiers that must be held (any side of each group).
    required: ModifierMask,
    pub key: TriggerKey,
}

impl KeyBinding {
    pub fn plain(key: TriggerKey) -> Self {
        Self { required: 0, key }
    }

    /// True if every required modifier is held; extra modifiers don't matter.
    pub fn modifiers_held(&self, held: ModifierMask) -> bool {
        MODIFIER_GROUPS
            .iter()
            .all(|&(group, _)| self.required & group == 0 || held & group != 0)
    }

    /// Number of required modifier groups; more means a more specific binding.
    pub fn specificity(&self) -> u32 {
        self.required.count_ones()
    }

    pub fn requires_caps_lock(&self) -> bool {
        self.required & CAPS_LOCK != 0
    }

    /// True if the binding needs a modifier of the given group (`CTRL`, `ALT`, ...).
    pub fn requires(&self, group: ModifierMask) -> bool {
        self.required & group != 0
    }

    /// True if the held Ctrl/Alt/Shift/Win groups are exactly the required ones, so `F13`
    /// does not fire under `Ctrl+F13` and the other way round. Caps Lock is a toggle:
    /// it only has to be held if the binding asks for it.
    pub fn modifiers_exact(&self, held: ModifierMask) -> bool {
        let groups_match = [CTRL, ALT, SHIFT, WIN]
            .iter()
            .all(|&g| (self.required & g != 0) == (held & g != 0));
        groups_match && (self.required & CAPS_LOCK == 0 || held & CAPS_LOCK != 0)
    }

    /// True if `self` (which matches with extra modifiers allowed) fires whenever
    /// `other` is pressed with exactly its modifiers: same key, and `self` needs
    /// no modifier that `other` lacks.
    pub fn fires_with(&self, other: &KeyBinding) -> bool {
        self.key == other.key && self.required & !other.required == 0
    }
}

/// The Morse keys. Without a dash key the press duration decides between dot and dash;
/// with one, `key` always gives a dot and `dash_key` always a dash.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MorseKeys {
    pub key: KeyBinding,
    pub dash_key: Option<KeyBinding>,
}

/// Names of the held modifier groups in display order, e.g. `["Ctrl", "Shift"]`.
pub fn modifier_names(held: ModifierMask) -> Vec<&'static str> {
    MODIFIER_GROUPS
        .iter()
        .filter(|&&(group, _)| held & group != 0)
        .map(|&(_, name)| name)
        .collect()
}

/// Group mask of a modifier name, German names included; shared by Morse keys and macros.
pub(crate) fn modifier_group(name: &str) -> Option<ModifierMask> {
    match name.to_ascii_lowercase().as_str() {
        "ctrl" | "control" | "strg" => Some(CTRL),
        "alt" => Some(ALT),
        "shift" | "umschalt" => Some(SHIFT),
        "win" | "windows" | "meta" | "super" => Some(WIN),
        "capslock" | "caps" | "feststell" => Some(CAPS_LOCK),
        _ => None,
    }
}

impl fmt::Display for KeyBinding {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for name in modifier_names(self.required) {
            write!(f, "{name}+")?;
        }
        write!(f, "{}", self.key)
    }
}

impl FromStr for KeyBinding {
    type Err = String;

    /// `Mod+Mod+Key`, where the key is any trigger name; case and blanks are ignored.
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let parts: Vec<&str> = s.split('+').map(str::trim).collect();
        let (key, modifiers) = parts.split_last().ok_or("leere Tastenkombination")?;
        let mut required = 0;
        for name in modifiers {
            required |=
                modifier_group(name).ok_or_else(|| format!("Unbekannte Zusatztaste '{name}'"))?;
        }
        let key = key.parse::<TriggerKey>().map_err(|e| e.to_string())?;
        Ok(Self { required, key })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn bindings_parse_and_print_in_canonical_form() {
        let caps_q: KeyBinding = " capslock + vk81 ".parse().unwrap();
        assert_eq!(caps_q.to_string(), "CapsLock+VK81");
        assert!(caps_q.requires_caps_lock());
        let shortcut: KeyBinding = "Shift+Strg+Pause".parse().unwrap();
        assert_eq!(shortcut.to_string(), "Ctrl+Shift+Pause");
        assert_eq!(
            "F8".parse::<KeyBinding>(),
            Ok(KeyBinding::plain(TriggerKey::F8))
        );
        for bad in ["", "Ctrl+", "Hyper+F9", "Ctrl+Foo"] {
            assert!(bad.parse::<KeyBinding>().is_err(), "{bad}");
        }
    }

    #[test]
    fn binding_needs_every_modifier_group_on_any_side() {
        let binding: KeyBinding = "Ctrl+CapsLock+F9".parse().unwrap();
        let left_ctrl = 0b01;
        let right_ctrl = 0b10;
        assert!(!binding.modifiers_held(0));
        assert!(!binding.modifiers_held(left_ctrl));
        assert!(binding.modifiers_held(left_ctrl | CAPS_LOCK));
        assert!(
            binding.modifiers_held(right_ctrl | CAPS_LOCK | SHIFT),
            "extra modifiers are fine"
        );
        assert!(KeyBinding::plain(TriggerKey::F9).modifiers_held(CTRL));
    }

    #[test]
    fn modifier_names_follow_display_order() {
        assert_eq!(
            modifier_names(CAPS_LOCK | SHIFT | 0b01),
            vec!["Ctrl", "Shift", "CapsLock"]
        );
        assert!(modifier_names(0).is_empty());
    }

    #[test]
    fn every_name_round_trips() {
        for &t in TriggerKey::ALL {
            assert_eq!(t.name().parse::<TriggerKey>(), Ok(t));
        }
    }

    #[test]
    fn parsing_is_case_insensitive_and_trims() {
        assert_eq!(" f8 ".parse::<TriggerKey>(), Ok(TriggerKey::F8));
        assert_eq!(
            "mouseright".parse::<TriggerKey>(),
            Ok(TriggerKey::MouseRight)
        );
    }

    #[test]
    fn unknown_name_lists_alternatives() {
        let err = "Hyper".parse::<TriggerKey>().unwrap_err();
        assert!(err.to_string().contains("ScrollLock"));
    }

    #[test]
    fn names_are_unique() {
        let names: HashSet<_> = TriggerKey::ALL.iter().map(|t| t.name()).collect();
        assert_eq!(names.len(), TriggerKey::ALL.len());
    }

    #[test]
    fn mouse_detection() {
        assert!(TriggerKey::MouseLeft.is_mouse());
        assert!(!TriggerKey::Space.is_mouse());
    }

    #[test]
    fn any_key_and_extra_mouse_buttons_parse_and_round_trip() {
        assert_eq!("VK65".parse::<TriggerKey>(), Ok(TriggerKey::Vk(65)));
        assert_eq!(" vk255 ".parse::<TriggerKey>(), Ok(TriggerKey::Vk(255)));
        assert_eq!(
            "Mouse5".parse::<TriggerKey>(),
            Ok(TriggerKey::MouseButton(5))
        );
        assert_eq!(TriggerKey::Vk(65).name(), "VK65");
        assert_eq!(TriggerKey::MouseButton(4).name(), "Mouse4");
        assert!(TriggerKey::MouseButton(4).is_mouse());
        assert!(!TriggerKey::Vk(65).is_mouse());
    }

    #[test]
    fn invalid_dynamic_names_are_rejected() {
        for bad in [
            "VK0", "VK256", "VK", "Mouse3", "Mouse0", "Mouse99", "Mouse", "Mousex",
        ] {
            assert!(bad.parse::<TriggerKey>().is_err(), "{bad}");
        }
    }

    #[test]
    fn f13_to_f24_are_named_keys_for_their_virtual_key_codes() {
        for n in 13..=24u16 {
            let name = format!("F{n}");
            let key: TriggerKey = name.parse().unwrap();
            assert_eq!(key, TriggerKey::Vk(124 + n - 13), "{name}");
            assert_eq!(key.name(), name);
            assert_eq!(key.name().parse::<TriggerKey>(), Ok(key));
            assert_eq!(
                format!("VK{}", 124 + n - 13).parse::<TriggerKey>(),
                Ok(key),
                "old spelling still parses"
            );
        }
        assert_eq!(" f13 ".parse::<TriggerKey>(), Ok(TriggerKey::Vk(124)));
        assert_eq!(
            "Ctrl+Alt+f24".parse::<KeyBinding>().unwrap().to_string(),
            "Ctrl+Alt+F24"
        );
        for bad in ["F0", "F25", "F100", "F013", "F+13", "F"] {
            assert!(bad.parse::<TriggerKey>().is_err(), "{bad}");
        }
        assert_eq!(TriggerKey::Vk(123).name(), "VK123");
        assert_eq!(TriggerKey::Vk(136).name(), "VK136");
    }

    #[test]
    fn exact_modifiers_tell_f13_from_ctrl_f13() {
        let plain: KeyBinding = "F13".parse().unwrap();
        let ctrl: KeyBinding = "Ctrl+F13".parse().unwrap();
        let all: KeyBinding = "Ctrl+Alt+Shift+Win+F13".parse().unwrap();
        let (left_ctrl, right_ctrl, alt, shift, win) = (0b01, 0b10, 0b100, 0b1_0000, 0b100_0000);
        assert!(plain.modifiers_exact(0));
        assert!(!plain.modifiers_exact(left_ctrl));
        assert!(ctrl.modifiers_exact(left_ctrl));
        assert!(
            ctrl.modifiers_exact(left_ctrl | right_ctrl),
            "both sides are one group"
        );
        assert!(!ctrl.modifiers_exact(0));
        assert!(!ctrl.modifiers_exact(left_ctrl | shift));
        assert!(all.modifiers_exact(left_ctrl | alt | shift | win));
        assert!(!all.modifiers_exact(left_ctrl | alt | shift));
        assert!(
            plain.modifiers_exact(CAPS_LOCK),
            "Caps Lock state is ignored unless required"
        );
        let caps: KeyBinding = "CapsLock+F13".parse().unwrap();
        assert!(!caps.modifiers_exact(0));
        assert!(caps.modifiers_exact(CAPS_LOCK));
    }

    #[test]
    fn fires_with_finds_shadowing_bindings() {
        let b = |s: &str| s.parse::<KeyBinding>().unwrap();
        assert!(b("F13").fires_with(&b("F13")));
        assert!(
            b("F13").fires_with(&b("Ctrl+F13")),
            "F13 also fires with Ctrl held"
        );
        assert!(!b("Ctrl+F13").fires_with(&b("F13")));
        assert!(!b("F13").fires_with(&b("F14")));
        assert!(!b("Ctrl+F13").fires_with(&b("Alt+F13")));
    }

    #[test]
    fn default_is_scroll_lock() {
        assert_eq!(TriggerKey::default(), TriggerKey::ScrollLock);
    }
}
