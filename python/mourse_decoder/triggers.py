"""Display names for Morse triggers (no tkinter).

A trigger name is the contract with the Rust hook: a standard name (Space, F8,
MouseRight), VK<code> for any key (Windows virtual-key code) or Mouse<n> for
extra mouse buttons from Mouse4 on. The hook captures them globally, not Tk.
"""

from __future__ import annotations

import re

from .i18n import tr

_VK = re.compile(r"^VK(\d+)$", re.IGNORECASE)
_MOUSE = re.compile(r"^Mouse(\d+)$", re.IGNORECASE)

# Esc (VK27) cancels while waiting for input, so it can't be a trigger
CANCEL_TOKEN = "VK27"

_VK_NAMES: dict[int, str] = {
    8: "Backspace", 9: "Tab", 12: "Clear", 13: "Enter", 19: "Pause", 20: "Caps Lock", 27: "Esc", 32: "Space",
    33: "Page Up", 34: "Page Down", 35: "End", 36: "Home", 37: "Left", 38: "Up", 39: "Right", 40: "Down",
    41: "Select", 42: "Print", 43: "Execute", 44: "Print Screen", 45: "Insert", 46: "Delete", 47: "Help",
    91: "Left Win", 92: "Right Win", 93: "Menu", 95: "Sleep",
    106: "Numpad *", 107: "Numpad +", 108: "Numpad Sep", 109: "Numpad -", 110: "Numpad .", 111: "Numpad /",
    144: "Num Lock", 145: "Scroll Lock", 160: "Left Shift", 161: "Right Shift", 162: "Left Ctrl",
    163: "Right Ctrl", 164: "Left Alt", 165: "Right Alt", 166: "Browser Back", 167: "Browser Forward",
    168: "Browser Refresh", 169: "Browser Stop", 170: "Browser Search", 171: "Browser Favorites",
    172: "Browser Home", 173: "Mute", 174: "Volume Down", 175: "Volume Up", 176: "Next Track",
    177: "Previous Track", 178: "Stop Media", 179: "Play/Pause", 180: "Mail", 181: "Media Select",
    182: "App 1", 183: "App 2", 186: ";", 187: "=", 188: ",", 189: "-", 190: ".", 191: "/", 192: "`",
    219: "[", 220: "\\", 221: "]", 222: "'", 226: "<",
    **{c: chr(c) for c in range(48, 58)},           # 0-9
    **{c: chr(c) for c in range(65, 91)},           # A-Z
    **{96 + i: f"Numpad {i}" for i in range(10)},
    **{111 + i: f"F{i}" for i in range(1, 25)},
}


def trigger_label(name: str) -> str:
    """Readable name in the current language for any valid trigger."""
    if match := _MOUSE.match(name):
        return tr("key.Mouse", n=int(match.group(1)))
    if match := _VK.match(name):
        code = int(match.group(1))
        return _VK_NAMES.get(code) or tr("key.Other", code=code)
    key = f"key.{name}"
    label = tr(key)
    return name if label == key else label


# Tokens the hook reports for modifier keys (AltGr counts as Alt)
MODIFIER_TOKENS: dict[str, str] = {
    "ControlRight": "Ctrl", "VK162": "Ctrl", "VK163": "Ctrl",
    "VK160": "Shift", "VK161": "Shift",
    "VK164": "Alt", "VK165": "Alt", "AltGr": "Alt",
    "VK91": "Win", "VK92": "Win",
}
MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")


def build_shortcut(modifiers: set[str], key: str) -> str:
    """e.g. ({"Shift", "Ctrl"}, "Pause") -> "Ctrl+Shift+Pause" (fixed modifier order)."""
    return "+".join([m for m in MODIFIER_ORDER if m in modifiers] + [key])


def shortcut_label(spec: str) -> str:
    """Readable form of a shortcut such as "Ctrl+Shift+Pause", with a translated key name."""
    if not spec.strip():
        return ""
    *modifiers, key = (part.strip() for part in spec.split("+"))
    return " + ".join([m.capitalize() if m.lower() != "win" else "Win" for m in modifiers] + [trigger_label(key)])
