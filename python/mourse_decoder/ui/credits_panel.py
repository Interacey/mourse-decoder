"""Credits page: developer, designer and the donate button."""

from __future__ import annotations

import tkinter as tk
import webbrowser
from collections.abc import Callable

from PIL import ImageTk

from .. import __version__
from ..easter_egg import bind_sos
from ..i18n import tr
from . import theme
from .icon import create_icon
from .theme import Button, Card, label

DEVELOPER = "Anton Schild aka Interacey"
DESIGNER = "Anton Schild aka Interacey"
DONATE_URL = "https://www.paypal.me/AntonSchild"


class CreditsPanel(tk.Frame):
    def __init__(self, master: tk.Misc, on_easter_egg: Callable[[], None] | None = None) -> None:
        super().__init__(master, bg=theme.P.bg)
        self.columnconfigure(0, weight=1)

        header = tk.Frame(self, bg=theme.P.bg)
        header.grid(row=0, column=0, sticky="w", pady=(0, 22))
        self._logo = ImageTk.PhotoImage(create_icon(True, 84))
        logo = tk.Label(header, image=self._logo, bg=theme.P.bg)
        logo.pack(side="left", padx=(0, 18))
        if on_easter_egg is not None:
            bind_sos(logo, on_easter_egg)
        text = tk.Frame(header, bg=theme.P.bg)
        text.pack(side="left")
        label(text, tr("app.name"), size=18, weight="bold").pack(anchor="w")
        label(text, tr("credits.tagline"), muted=True).pack(anchor="w", pady=(2, 0))
        label(text, tr("credits.version", v=__version__), size=9, muted=True).pack(anchor="w", pady=(2, 0))

        card = Card(self)
        card.grid(row=1, column=0, sticky="ew")
        label(card.add_row(tr("credits.developer")), DEVELOPER, weight="bold").pack()
        label(card.add_row(tr("credits.designer")), DESIGNER, weight="bold").pack()

        label(self, tr("credits.support"), size=9, weight="bold", muted=True).grid(row=2, column=0, sticky="w", pady=(24, 6))
        donate = Card(self)
        donate.grid(row=3, column=0, sticky="ew")
        inner = tk.Frame(donate.body, bg=theme.P.surface)
        inner.pack(fill="x", padx=20, pady=16)
        label(inner, tr("credits.donate.text"), wraplength=520, justify="left").pack(anchor="w")
        Button(inner, tr("credits.donate"), self._donate, variant="primary", min_width=190).pack(anchor="w", pady=(14, 0))

    @staticmethod
    def _donate() -> None:
        webbrowser.open(DONATE_URL)
