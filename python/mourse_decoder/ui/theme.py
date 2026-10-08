"""Shared look: calm, very round, with a glass feel.

Light uses #F5F5F5 backgrounds, light gray cards and a charcoal selection color. Dark
uses anthracite with light gray text. Glass means soft shadows, a rim that glows at
the top and fades out, and a slight sheen on buttons, all drawn antialiased with
Pillow. On Windows 11 the title bar gets the real Mica backdrop. mode="system"
follows the Windows setting. No emojis, icons are plain lines.
"""

from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk


@dataclass(frozen=True)
class Palette:
    bg: str
    surface: str       # cards ("glass")
    field: str         # input fields
    sidebar: str
    border: str
    glass: str         # glowing rim at the top
    text: str
    muted: str
    accent: str        # selection color: main button, switch
    accent_hover: str
    accent_fg: str     # text on accent fills
    accent_text: str   # accent color for text
    soft: str          # secondary buttons
    soft_hover: str
    soft_fg: str
    hover: str
    select: str
    danger: str
    knob: str          # switch knob (off)
    knob_on: str       # switch knob (on)
    shadow: float


LIGHT = Palette(
    bg="#F5F5F5", surface="#E8E8EA", field="#F5F5F5", sidebar="#F5F5F5", border="#D3D3D7", glass="#FFFFFF",
    text="#22252A", muted="#6B6F76", accent="#2b7bd6", accent_hover="#50555C", accent_fg="#F5F5F5",
    accent_text="#2F3338", soft="#DCDCE0", soft_hover="#D0D0D5", soft_fg="#22252A", hover="#E2E2E5",
    select="#E0E0E4", danger="#C62828", knob="#F5F5F5", knob_on="#F5F5F5", shadow=0.12,
)
DARK = Palette(
    bg="#2A2D31", surface="#34383D", field="#2B2F33", sidebar="#222528", border="#464B52", glass="#5C636B",
    text="#D9DCE0", muted="#9AA1A9", accent="#bfe6ff", accent_hover="#FFFFFF", accent_fg="#22262A",
    accent_text="#bfe6ff", soft="#454A51", soft_hover="#50565E", soft_fg="#E4E6E9", hover="#3F444A",
    select="#3F454C", danger="#E5645B", knob="#D5D8DC", knob_on="#2F3338", shadow=0.40,
)
# set by apply_theme; widgets read theme.P when they are created
P: Palette = LIGHT
FONT = "Segoe UI"
MONO = "Consolas"

CARD_RADIUS = 24
FIELD_RADIUS = 15
BUTTON_HEIGHT = 36
CARD_MARGIN = 6  # room for the soft shadow around cards


def system_prefers_dark() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def font(size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
    return (FONT, size, weight)


def is_dark() -> bool:
    return P is DARK


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


_images: dict[tuple, ImageTk.PhotoImage] = {}


def _render(
    w: int, h: int, radius: int, fill: str, border: str | None, hi: str | None, gloss: bool, shadow: float, margin: int
) -> Image.Image:
    scale = 4 if w * h < 20000 else 2
    width, height = w * scale, h * scale
    m = margin * scale
    box = (m, m, max(width - m - 1, m + 1), max(height - m - 1, m + 1))
    r = min(radius * scale, (min(box[2] - box[0], box[3] - box[1])) // 2)
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=r, fill=255)

    if shadow > 0 and margin > 0:  # soft shadow, nudged down a little
        dy = max(margin // 2, 1) * scale
        blob = Image.new("L", (width, height), 0)
        ImageDraw.Draw(blob).rounded_rectangle((box[0], box[1] + dy, box[2], box[3] + dy), radius=r, fill=255)
        blob = blob.filter(ImageFilter.GaussianBlur(margin * scale / 2.2)).point(lambda v: int(v * shadow))
        shade = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        shade.putalpha(blob)
        out = Image.alpha_composite(out, shade)

    layer = Image.new("RGBA", (width, height), (*_rgb(fill), 255))
    vertical = Image.linear_gradient("L").resize((width, height))  # 0 at the top, 255 at the bottom
    if gloss:  # sheen on the upper half
        sheen = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        sheen.putalpha(vertical.point(lambda v: int(max(0, 125 - v) * 0.5)))
        layer = Image.alpha_composite(layer, sheen)
    layer.putalpha(mask)
    out = Image.alpha_composite(out, layer)

    if border:  # rim: bright (hi) at the top, fading into the border color
        inner = Image.new("L", (width, height), 0)
        ImageDraw.Draw(inner).rounded_rectangle(
            (box[0] + scale, box[1] + scale, box[2] - scale, box[3] - scale), radius=max(r - scale, 0), fill=255
        )
        ring = ImageChops.subtract(mask, inner)
        top = Image.new("RGBA", (width, height), (*_rgb(hi or border), 255))
        bottom = Image.new("RGBA", (width, height), (*_rgb(border), 255))
        edge = Image.composite(bottom, top, vertical)
        edge.putalpha(ring)
        out = Image.alpha_composite(out, edge)

    return out.resize((w, h), Image.LANCZOS)


def shape_image(
    w: int, h: int, radius: int, fill: str, border: str | None = None, hi: str | None = None,
    gloss: bool = False, shadow: float = 0.0, margin: int = 0,
) -> ImageTk.PhotoImage:
    """Rounded shape as a cached image. ``margin`` is room for the shadow."""
    key = (w, h, radius, fill, border, hi, gloss, shadow, margin)
    image = _images.get(key)
    if image is None:
        if len(_images) > 300:
            _images.clear()
        image = _images[key] = ImageTk.PhotoImage(_render(w, h, radius, fill, border, hi, gloss, shadow, margin))
    return image


def apply_theme(root: tk.Misc, mode: str = "system") -> None:
    """Pick the palette (system/light/dark) and set all ttk styles."""
    global P, FONT
    dark = system_prefers_dark() if mode == "system" else mode == "dark"
    P = DARK if dark else LIGHT
    _images.clear()
    families = set(tkfont.families(root))
    FONT = next(
        (f for f in ("Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", "Helvetica") if f in families),
        "TkDefaultFont",
    )

    root.configure(bg=P.bg)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=FONT, size=10)
    root.option_add("*TCombobox*Listbox.background", P.field)
    root.option_add("*TCombobox*Listbox.foreground", P.text)
    root.option_add("*TCombobox*Listbox.selectBackground", P.select)
    root.option_add("*TCombobox*Listbox.selectForeground", P.text)
    root.option_add("*TCombobox*Listbox.font", font(10))

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=P.surface, foreground=P.text, font=font(10), bordercolor=P.surface)

    # fields have no border of their own, the rounded field_box draws it
    field = {
        "fieldbackground": P.field, "background": P.field, "foreground": P.text, "bordercolor": P.field,
        "lightcolor": P.field, "darkcolor": P.field, "arrowcolor": P.muted, "padding": 7, "relief": "flat",
        "insertcolor": P.text, "borderwidth": 0,
    }
    for name in ("TCombobox", "TSpinbox", "TEntry"):
        style.configure(name, **field)
        style.map(name, bordercolor=[("focus", P.field)], lightcolor=[("focus", P.field)], darkcolor=[("focus", P.field)])
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", P.field)],
        foreground=[("readonly", P.text)],
        selectbackground=[("readonly", P.field)],
        selectforeground=[("readonly", P.text)],
    )

    style.configure(
        "Vertical.TScrollbar", background=P.border, troughcolor=P.bg, bordercolor=P.bg,
        lightcolor=P.border, darkcolor=P.border, arrowcolor=P.bg, arrowsize=10, relief="flat",
    )
    style.map("Vertical.TScrollbar", background=[("active", P.muted)])

    style.configure(
        "Treeview", background=P.surface, fieldbackground=P.surface, foreground=P.text, rowheight=32,
        borderwidth=0, font=font(10),
    )
    style.configure("Treeview.Heading", background=P.surface, foreground=P.muted, relief="flat", font=font(9, "bold"))
    style.map("Treeview", background=[("selected", P.select)], foreground=[("selected", P.text)])
    style.map("Treeview.Heading", background=[("active", P.surface)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # no border


def apply_window_chrome(root: tk.Tk) -> None:
    """Dark or light title bar on Windows 10/11, with Mica on 11. Does nothing elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwm = ctypes.windll.dwmapi

        def set_attribute(attribute: int, value: int) -> None:
            data = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))

        set_attribute(20, 1 if is_dark() else 0)  # immersive dark mode
        set_attribute(38, 2)  # system backdrop = Mica
        r, g, b = _rgb(P.bg)
        set_attribute(35, r | (g << 8) | (b << 16))  # caption color matching the background
    except Exception:  # noqa: BLE001 – purely cosmetic
        pass


def clip_round(window: tk.Misc, width: int, height: int, radius: int) -> None:
    """Clip a borderless window to a rounded shape so no square corners show (Windows only)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
        ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
    except (AttributeError, OSError):
        pass

def _bg_of(widget: tk.Misc) -> str:
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return P.bg


def _round_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, r: float, **kw: object) -> int:
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2,
           x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def label(master: tk.Misc, text: str = "", size: int = 10, weight: str = "normal", muted: bool = False, accent: bool = False, **kw: object) -> tk.Label:
    color = P.accent_text if accent else (P.muted if muted else P.text)
    return tk.Label(master, text=text, bg=_bg_of(master), fg=color, font=font(size, weight), anchor="w", **kw)


class Button(tk.Canvas):
    """Pill button with a sheen. Variants: primary (selection color), secondary (gray), plain (text only)."""

    def __init__(self, master: tk.Misc, text: str, command=None, variant: str = "secondary", min_width: int = 0) -> None:
        self._font = tkfont.Font(family=FONT, size=10, weight="bold" if variant == "primary" else "normal")
        self._text, self._command, self._variant = text, command, variant
        self._min_width = min_width
        self._width = self._measure(text)
        super().__init__(master, width=self._width, height=BUTTON_HEIGHT, bg=_bg_of(master), highlightthickness=0, bd=0)
        self._enabled, self._hover, self._down = True, False, False
        self.configure(cursor="hand2")
        self.bind("<Enter>", lambda _e: self._set(hover=True))
        self.bind("<Leave>", lambda _e: self._set(hover=False, down=False))
        self.bind("<ButtonPress-1>", lambda _e: self._set(down=True))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _measure(self, text: str) -> int:
        return max(self._min_width, self._font.measure(text) + 40)

    def _set(self, hover: bool | None = None, down: bool | None = None) -> None:
        if hover is not None:
            self._hover = hover
        if down is not None:
            self._down = down
        self._draw()

    def _release(self, event: tk.Event) -> None:
        inside = 0 <= event.x <= self._width and 0 <= event.y <= BUTTON_HEIGHT
        fire = self._down and inside and self._enabled and self._command is not None
        self._set(down=False)
        if fire:
            self._command()

    def _colors(self) -> tuple[str | None, str, str | None]:
        """(fill or None, text color, border color)"""
        if not self._enabled:
            return P.hover, P.muted, None
        active = self._hover or self._down
        if self._variant == "primary":
            return (P.accent_hover if active else P.accent), P.accent_fg, P.accent
        if self._variant == "plain":
            return (P.hover if active else None), P.accent_text, None
        return (P.soft_hover if active else P.soft), P.soft_fg, P.border

    def _draw(self) -> None:
        self.delete("all")
        fill, fg, border = self._colors()
        if fill is not None:
            self._shape = shape_image(
                self._width, BUTTON_HEIGHT, BUTTON_HEIGHT // 2, fill, border, P.glass if border else None, gloss=True
            )
            self.create_image(0, 0, anchor="nw", image=self._shape)
        self.create_text(self._width / 2, BUTTON_HEIGHT / 2, text=self._text, fill=fg, font=self._font)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_text(self, text: str) -> None:
        self._text = text
        self._width = self._measure(text)
        self.configure(width=self._width)
        self._draw()


class Switch(tk.Canvas):
    """On/off switch bound to a BooleanVar."""

    W, H = 52, 30

    def __init__(self, master: tk.Misc, variable: tk.BooleanVar) -> None:
        super().__init__(master, width=self.W, height=self.H, bg=_bg_of(master), highlightthickness=0, bd=0, cursor="hand2")
        self._var = variable
        variable.trace_add("write", lambda *_: self._draw())
        self.bind("<ButtonRelease-1>", lambda _e: variable.set(not variable.get()))
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        on = bool(self._var.get())
        track = P.accent if on else P.border
        self._track = shape_image(self.W, self.H, self.H // 2, track, P.border if not on else P.accent, P.glass, gloss=on)
        self.create_image(0, 0, anchor="nw", image=self._track)
        size, pad = 26, 2
        self._knob = shape_image(size + 2 * pad, size + 2 * pad, (size + 2 * pad) // 2, P.knob_on if on else P.knob, P.border, P.glass, shadow=0.35, margin=pad)
        x = self.W - (size + 2 * pad) - 1 if on else 1
        self.create_image(x, (self.H - (size + 2 * pad)) // 2, anchor="nw", image=self._knob)


class RoundedBox(tk.Canvas):
    """Glass surface with shadow and glowing rim; put content in ``.body``.

    fit=True makes the height follow the content (cards), fit=False fills the space
    it is given (lists, editors). ``margin`` is room for the shadow, shadow=False drops it.
    """

    def __init__(
        self, master: tk.Misc, radius: int = CARD_RADIUS, pad: int = 0, fit: bool = True,
        margin: int = CARD_MARGIN, shadow: bool = True, fill: str | None = None,
    ) -> None:
        super().__init__(master, bg=_bg_of(master), highlightthickness=0, bd=0, height=10)
        self._radius, self._fit, self._margin = radius, fit, margin
        # the square body must not cover the rounded corners, so inset by about 30% of the radius
        self._inset = margin + max(pad, int(radius * 0.3) + 1)
        self._shadow = P.shadow if shadow and margin else 0.0
        # minimum height: Tk only maps an embedded window whose origin is inside the visible area
        self.configure(height=2 * self._inset + 4)
        self._fill = fill or P.surface
        self.body = tk.Frame(self, bg=self._fill)
        self._window = self.create_window(self._inset, self._inset, anchor="nw", window=self.body)
        self._size = (0, 0)
        self._job: str | None = None
        self._image: ImageTk.PhotoImage | None = None
        self.bind("<Configure>", self._on_resize)
        if fit:
            self.body.bind("<Configure>", self._on_body)

    def _on_body(self, _event: tk.Event) -> None:
        self.configure(height=self.body.winfo_reqheight() + 2 * self._inset)

    def _on_resize(self, event: tk.Event) -> None:
        w, h = event.width, event.height
        if (w, h) == self._size or w <= 2 * self._margin + 4 or h <= 2 * self._margin + 4:
            return
        first = self._size == (0, 0)
        self._size = (w, h)
        options = {"width": max(w - 2 * self._inset, 1)}
        if not self._fit:
            options["height"] = max(h - 2 * self._inset, 1)
        self.itemconfigure(self._window, **options)
        if first:
            self._redraw()
        else:  # while resizing, don't render every intermediate frame
            if self._job is not None:
                self.after_cancel(self._job)
            self._job = self.after(40, self._redraw)

    def _redraw(self) -> None:
        self._job = None
        w, h = self._size
        self._image = shape_image(
            w, h, self._radius, self._fill, P.border, P.glass, shadow=self._shadow, margin=self._margin
        )
        self.delete("bg")
        self.create_image(0, 0, anchor="nw", image=self._image, tags="bg")
        self.tag_lower("bg")


class Card(RoundedBox):
    """Round glass group; each row has a title on the left and a control on the right."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.body.columnconfigure(0, weight=1)
        self._rows = 0

    def add_row(self, title: str, hint: str = "") -> tk.Frame:
        """Add a row and return the container for its control (right side)."""
        if self._rows:
            tk.Frame(self.body, bg=P.border, height=1).grid(row=self._rows * 2 - 1, column=0, sticky="ew", padx=24)
        row = tk.Frame(self.body, bg=P.surface)
        row.grid(row=self._rows * 2, column=0, sticky="ew", padx=24, pady=12)
        row.columnconfigure(0, weight=1)
        text = tk.Frame(row, bg=P.surface)
        text.grid(row=0, column=0, sticky="w")
        label(text, title).pack(anchor="w")
        if hint:
            label(text, hint, size=9, muted=True).pack(anchor="w")
        control = tk.Frame(row, bg=P.surface)
        control.grid(row=0, column=1, sticky="e", padx=(16, 0))
        self._rows += 1
        return control


def field_box(master: tk.Misc, factory, **kw: object):
    """Combobox/spinbox in a rounded shell. Returns (box, widget); place the box."""
    box = RoundedBox(master, radius=FIELD_RADIUS, pad=3, margin=0, shadow=False, fill=P.field)
    widget = factory(box.body, **kw)
    widget.pack(fill="x")
    return box, widget


class ScrollFrame(tk.Frame):
    """Vertically scrollable area, content goes in ``.body``. Scrollbar only when needed, mouse wheel works."""

    def __init__(self, master: tk.Misc) -> None:
        bg = _bg_of(master)
        super().__init__(master, bg=bg)
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=24)
        self._bar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._bar.set)
        self.body = tk.Frame(self._canvas, bg=bg)
        self._window = self._canvas.create_window(0, 0, anchor="nw", window=self.body)
        self._canvas.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", lambda _e: self._update())
        self._canvas.bind("<Configure>", self._on_canvas)
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _on_canvas(self, event: tk.Event) -> None:
        self._canvas.itemconfigure(self._window, width=event.width)
        self._update()

    def _scrollable(self) -> bool:
        return self.body.winfo_reqheight() > self._canvas.winfo_height()

    def _update(self) -> None:
        self._canvas.configure(scrollregion=(0, 0, self.body.winfo_reqwidth(), self.body.winfo_reqheight()))
        if self._scrollable():
            if not self._bar.winfo_ismapped():
                self._bar.pack(side="right", fill="y", padx=(8, 0))
        else:
            self._bar.pack_forget()
            self._canvas.yview_moveto(0)

    def _on_wheel(self, event: tk.Event) -> None:
        if not self.winfo_exists() or not self._scrollable():
            return
        if str(event.widget).startswith(str(self)):
            self._canvas.yview_scroll(-2 if event.delta > 0 else 2, "units")


def text_box(master: tk.Misc, **kw: object) -> tk.Text:
    """Text input without its own border, put it in a RoundedBox (fit=False)."""
    options = dict(
        bg=P.surface, fg=P.text, insertbackground=P.text, selectbackground=P.select, selectforeground=P.text,
        relief="flat", bd=0, highlightthickness=0, padx=16, pady=14, font=(MONO, 10), undo=True,
    )
    options.update(kw)
    return tk.Text(master, **options)


def list_box(master: tk.Misc, **kw: object) -> tk.Listbox:
    """Listbox without its own border, put it in a RoundedBox (fit=False)."""
    options = dict(
        bg=P.surface, fg=P.text, selectbackground=P.select, selectforeground=P.text, relief="flat", bd=0,
        highlightthickness=0, activestyle="none", exportselection=False, font=font(10),
    )
    options.update(kw)
    return tk.Listbox(master, **options)


def boxed(master: tk.Misc, factory, pad: int = 10, **kw: object):
    """Create a widget (text_box, list_box, ...) inside a rounded, filling surface.

    Returns (box, widget): grid the box, the widget is the content.
    """
    box = RoundedBox(master, fit=False)
    widget = factory(box.body, **kw)
    widget.pack(fill="both", expand=True, padx=pad, pady=pad)
    return box, widget


def draw_glyph(canvas: tk.Canvas, name: str, cx: float, cy: float, color: str, tag: str = "glyph") -> None:
    """Draw an 18 px icon centered at (cx, cy)."""
    canvas.delete(tag)
    x, y = cx - 9, cy - 9
    line = dict(fill=color, width=2, capstyle="round", tags=tag)
    if name == "settings":  # three sliders
        for i, knob in enumerate((6, 12, 8)):
            yy = y + 3 + i * 6
            canvas.create_line(x, yy, x + 18, yy, **line)
            canvas.create_oval(x + knob - 2.5, yy - 2.5, x + knob + 2.5, yy + 2.5, fill=canvas.cget("bg"), outline=color, width=2, tags=tag)
    elif name == "alphabets":  # "Aa"
        canvas.create_text(cx, cy, text="Aa", fill=color, font=font(11, "bold"), tags=tag)
    elif name == "smileys":  # speech bubble
        canvas.create_polygon(x + 1, y + 2, x + 17, y + 2, x + 17, y + 12, x + 8, y + 12, x + 4, y + 17, x + 4, y + 12, x + 1, y + 12,
                              fill="", outline=color, width=2, joinstyle="round", tags=tag)
    elif name == "live":  # pulse line
        canvas.create_line(x, y + 9, x + 4, y + 9, x + 7, y + 2, x + 11, y + 16, x + 14, y + 9, x + 18, y + 9, joinstyle="round", **line)
    elif name == "translator":  # two rows of Morse marks
        for row, dot_first in ((5, True), (13, False)):
            yy = y + row
            dot_x, dash = (x + 2, (x + 8, x + 17)) if dot_first else (x + 16, (x + 1, x + 10))
            canvas.create_oval(dot_x - 2, yy - 2, dot_x + 2, yy + 2, fill=color, outline=color, tags=tag)
            canvas.create_line(dash[0], yy, dash[1], yy, **line)
    elif name == "credits":  # circle with an i
        canvas.create_oval(x + 1, y + 1, x + 17, y + 17, fill="", outline=color, width=2, tags=tag)
        canvas.create_line(x + 9, y + 8, x + 9, y + 13, **line)
        canvas.create_oval(x + 8, y + 4, x + 10, y + 6, fill=color, outline=color, tags=tag)
    elif name == "sidebar":  # window with a sidebar
        canvas.create_rectangle(x + 1, y + 2, x + 17, y + 16, fill="", outline=color, width=2, tags=tag)
        canvas.create_line(x + 7, y + 2, x + 7, y + 16, **line)
