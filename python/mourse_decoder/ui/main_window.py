"""Main window: sidebar (icons only when collapsed) and content area."""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable

from PIL import ImageTk

from ..controller import MorseController
from ..dictionary import DictionaryManager
from ..easter_egg import bind_sos
from ..i18n import set_language, tr
from ..settings import Settings
from . import theme
from .alphabet_panel import AlphabetPanel
from .battleship_window import BattleshipWindow
from .credits_panel import CreditsPanel
from .icon import create_icon
from .live_panel import LivePanel
from .settings_panel import SettingsPanel
from .smiley_panel import SmileyPanel
from .translator_panel import TranslatorPanel

log = logging.getLogger(__name__)

EXPANDED_WIDTH = 236
COLLAPSED_WIDTH = 76
ITEM_HEIGHT = 44


class _NavItem(tk.Canvas):
    """Sidebar entry: icon plus text, or just the icon when collapsed."""

    def __init__(self, master: tk.Misc, glyph: str, text: str, command: Callable[[], None]) -> None:
        super().__init__(master, height=ITEM_HEIGHT, bg=theme.P.sidebar, highlightthickness=0, bd=0, cursor="hand2")
        self._glyph, self._text = glyph, text
        self._selected = self._hover = self._collapsed = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", lambda _e: command())
        self.bind("<Enter>", lambda _e: self._set(hover=True))
        self.bind("<Leave>", lambda _e: self._set(hover=False))

    def _set(self, hover: bool) -> None:
        self._hover = hover
        self._draw()

    def select(self, selected: bool) -> None:
        self._selected = selected
        self._draw()

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self._draw()

    def _draw(self) -> None:
        width = self.winfo_width()
        self.delete("all")
        if self._selected:
            fill = theme.P.select
        elif self._hover:
            fill = theme.P.hover
        else:
            fill = ""
        if fill and width > 8:
            self._shape = theme.shape_image(
                width - 4, ITEM_HEIGHT - 4, 18, fill, theme.P.border if self._selected else None, theme.P.glass, gloss=True
            )
            self.create_image(2, 2, anchor="nw", image=self._shape)
        color = theme.P.accent_text if self._selected else theme.P.text
        cx = width / 2 if self._collapsed else 28
        theme.draw_glyph(self, self._glyph, cx, ITEM_HEIGHT / 2, color)
        if not self._collapsed and self._text:
            self.create_text(
                54, ITEM_HEIGHT / 2, text=self._text, anchor="w", fill=color,
                font=theme.font(10, "bold" if self._selected else "normal"),
            )


class MainWindow:
    """Builds the UI inside the existing tk.Tk root."""

    TABS = ("settings", "alphabets", "smileys", "live", "translator", "credits")
    _GLYPHS = {"settings": "settings", "alphabets": "alphabets", "smileys": "smileys", "live": "live", "translator": "translator", "credits": "credits"}

    def __init__(
        self,
        root: tk.Tk,
        controller: MorseController,
        dictionaries: DictionaryManager,
        apply_settings: Callable[[Settings], None],
        on_alphabets_changed: Callable[[], None],
        on_hide: Callable[[], None],
    ) -> None:
        self._root = root
        self._controller = controller
        self._dictionaries = dictionaries
        self._apply_settings = apply_settings
        self._on_alphabets_changed = on_alphabets_changed
        self._on_hide = on_hide
        self._page = "settings"
        self._icon_image: ImageTk.PhotoImage | None = None
        self._rendered: tuple[str, str] = ("", "")
        self._rebuild_pending = False
        self._battleship: BattleshipWindow | None = None

        root.geometry("1040x740")
        root.minsize(900, 700)
        root.protocol("WM_DELETE_WINDOW", on_hide)  # the close button hides to the tray instead of quitting
        self._build()

    def _build(self) -> None:
        root, settings = self._root, self._controller.settings
        for child in root.winfo_children():
            if child.winfo_class() != "Toplevel":  # keep the splash and dialogs
                child.destroy()
        set_language(settings.language)
        theme.apply_theme(root, settings.theme)
        theme.apply_window_chrome(root)
        self._rendered = (settings.language, settings.theme)
        root.title(tr("app.name"))
        self._icon_image = ImageTk.PhotoImage(create_icon(True, 64))
        root.iconphoto(True, self._icon_image)


        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # sidebar
        self._collapsed = settings.sidebar_collapsed
        self._sidebar = tk.Frame(root, bg=theme.P.sidebar)
        self._sidebar.grid(row=0, column=0, sticky="ns")
        self._sidebar.grid_propagate(False)
        self._sidebar.columnconfigure(0, weight=1)
        self._sidebar.rowconfigure(2, weight=1)

        self._brand = tk.Frame(self._sidebar, bg=theme.P.sidebar)
        self._brand.grid(row=0, column=0, sticky="ew", padx=14, pady=(22, 18))
        self._brand_logo = ImageTk.PhotoImage(create_icon(True, 34))
        self._brand_image = tk.Label(self._brand, image=self._brand_logo, bg=theme.P.sidebar)
        bind_sos(self._brand_image, self.open_battleship)  # easter egg: click SOS on the icon
        self._brand_title = tk.Label(
            self._brand, text=tr("app.name"), bg=theme.P.sidebar, fg=theme.P.text, font=theme.font(13, "bold")
        )

        self._nav_frame = tk.Frame(self._sidebar, bg=theme.P.sidebar)
        self._nav_frame.grid(row=1, column=0, sticky="ew", padx=10)

        bottom = tk.Frame(self._sidebar, bg=theme.P.sidebar)
        bottom.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 16))
        self._status = tk.Label(
            bottom, bg=theme.P.sidebar, fg=theme.P.muted, font=theme.font(9), anchor="w", justify="left", wraplength=180
        )
        self._toggle = _NavItem(bottom, "sidebar", "", self._toggle_sidebar)
        self._toggle.pack(side="bottom", fill="x")
        self._status.pack(side="bottom", fill="x", padx=14, pady=(0, 10))
        tk.Frame(root, bg=theme.P.border, width=1).grid(row=0, column=0, sticky="nse")

        # content
        content = tk.Frame(root, bg=theme.P.bg)
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)
        self._heading = tk.Label(content, bg=theme.P.bg, fg=theme.P.text, font=theme.font(24, "bold"), anchor="w")
        self._heading.grid(row=0, column=0, sticky="ew", padx=36, pady=(26, 14))
        stack = tk.Frame(content, bg=theme.P.bg)
        stack.grid(row=1, column=0, sticky="nsew", padx=36, pady=(0, 30))
        stack.columnconfigure(0, weight=1)
        stack.rowconfigure(0, weight=1)

        def changed() -> None:
            self.sync()
            self._on_alphabets_changed()

        self.settings_panel = SettingsPanel(stack, self._controller, self._dictionaries, self._apply_settings)
        self.alphabet_panel = AlphabetPanel(stack, self._controller, self._dictionaries, self._apply_settings, on_changed=changed)
        self.smiley_panel = SmileyPanel(stack, self._controller, self._dictionaries, self._apply_settings, on_changed=changed)
        self.live_panel = LivePanel(stack, self._controller)
        self.translator_panel = TranslatorPanel(stack, self._controller, self._dictionaries)
        self.credits_panel = CreditsPanel(stack, on_easter_egg=self.open_battleship)
        self._pages = {
            "settings": self.settings_panel,
            "alphabets": self.alphabet_panel,
            "smileys": self.smiley_panel,
            "live": self.live_panel,
            "translator": self.translator_panel,
            "credits": self.credits_panel,
        }
        self._nav: dict[str, _NavItem] = {}
        for key, page in self._pages.items():
            item = _NavItem(self._nav_frame, self._GLYPHS[key], tr(f"nav.{key}"), lambda k=key: self.show_tab(k))
            item.pack(fill="x", pady=2)
            self._nav[key] = item
            page.grid(row=0, column=0, sticky="nsew")
        self._apply_collapsed()
        self.show_tab(self._page)
        self._update_status()

    def _apply_collapsed(self) -> None:
        collapsed = self._collapsed
        self._sidebar.configure(width=COLLAPSED_WIDTH if collapsed else EXPANDED_WIDTH)
        self._brand_image.pack_forget()
        self._brand_title.pack_forget()
        if collapsed:
            self._brand_image.pack()
            self._status.pack_forget()
        else:
            self._brand_image.pack(side="left", padx=(6, 10))
            self._brand_title.pack(side="left")
            self._status.pack(side="bottom", fill="x", padx=14, pady=(0, 10), before=self._toggle)
        for item in (*self._nav.values(), self._toggle):
            item.set_collapsed(collapsed)

    def _toggle_sidebar(self) -> None:
        try:
            self._apply_settings(self._controller.settings.replace(sidebar_collapsed=not self._collapsed))
        except Exception:  # noqa: BLE001 – the layout change still happened, only saving failed
            log.exception("Seitenleiste: Einstellung nicht gespeichert")
            self._collapsed = not self._collapsed
            self._apply_collapsed()

    def open_battleship(self) -> None:
        """Easter egg: battleship, one window at a time."""
        if self._battleship is not None and self._battleship.winfo_exists():
            self._battleship.lift()
            return
        self._battleship = BattleshipWindow(self._root)

    def show_tab(self, name: str) -> None:
        self._page = name
        self._pages[name].tkraise()
        self._heading.configure(text=tr(f"nav.{name}"))
        for key, item in self._nav.items():
            item.select(key == name)

    def _update_status(self) -> None:
        s = self._controller.settings
        names = {a.id: a.name for a in self._dictionaries.available()}
        state = tr("status.active") if s.enabled else tr("status.paused")
        lines = [state, names.get(s.alphabet, s.alphabet)]
        if s.block_input and s.enabled:
            lines.append(tr("status.blocked"))
        self._status.configure(text="\n".join(lines))

    def sync(self) -> None:
        """Refresh the pages, or rebuild everything when language or theme changed."""
        s = self._controller.settings
        if (s.language, s.theme) != self._rendered:
            if not self._rebuild_pending:
                self._rebuild_pending = True
                self._root.after(0, self._rebuild)  # don't destroy widgets in the middle of a click handler
            return
        if s.sidebar_collapsed != self._collapsed:
            self._collapsed = s.sidebar_collapsed
            self._apply_collapsed()
        self.settings_panel.refresh()
        self.alphabet_panel.refresh()
        self.smiley_panel.refresh()
        self.live_panel.refresh()
        self.translator_panel.refresh()
        self._update_status()

    def _rebuild(self) -> None:
        self._rebuild_pending = False
        self._build()
        self._on_alphabets_changed()  # tray menu in the new language

    def show(self) -> None:
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()

    def hide(self) -> None:
        self._root.withdraw()

    @property
    def visible(self) -> bool:
        return self._root.state() != "withdrawn"
