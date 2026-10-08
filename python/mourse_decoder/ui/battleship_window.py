"""Easter egg window: battleship against the bot (logic in battleship.py)."""

from __future__ import annotations

import tkinter as tk

from ..battleship import SIZE, Board, Cell, Game, ShotResult
from ..i18n import tr
from . import theme
from .theme import Button, RoundedBox, label

CELL = 32
LABEL = 22
BOT_DELAY_MS = 750


class _Grid(tk.Canvas):
    """A 10x10 grid. ``own`` shows the ships, otherwise only shots and sunk ships."""

    def __init__(self, master: tk.Misc, board: Board, own: bool, on_click=None) -> None:
        size = LABEL + SIZE * CELL + 2
        super().__init__(master, width=size, height=size, bg=theme.P.surface, highlightthickness=0, bd=0)
        self.board, self.own, self._on_click = board, own, on_click
        self._hover: Cell | None = None
        self.interactive = False
        if on_click is not None:
            self.bind("<Motion>", self._motion)
            self.bind("<Leave>", lambda _e: self._set_hover(None))
            self.bind("<ButtonPress-1>", self._click)
        self.redraw()

    def _cell_at(self, event: tk.Event) -> Cell | None:
        x, y = (event.x - LABEL) // CELL, (event.y - LABEL) // CELL
        return (x, y) if 0 <= x < SIZE and 0 <= y < SIZE and event.x >= LABEL and event.y >= LABEL else None

    def _motion(self, event: tk.Event) -> None:
        self._set_hover(self._cell_at(event))

    def _set_hover(self, cell: Cell | None) -> None:
        if cell != self._hover:
            self._hover = cell
            self.redraw()

    def _click(self, event: tk.Event) -> None:
        cell = self._cell_at(event)
        if cell is not None and self.interactive and self._on_click is not None:
            self._on_click(cell)

    def _rect(self, cell: Cell, inset: int = 2) -> tuple[int, int, int, int]:
        x0, y0 = LABEL + cell[0] * CELL, LABEL + cell[1] * CELL
        return x0 + inset, y0 + inset, x0 + CELL - inset, y0 + CELL - inset

    def redraw(self) -> None:
        self.delete("all")
        p = theme.P
        for i in range(SIZE):
            self.create_text(LABEL + i * CELL + CELL / 2, LABEL / 2, text="ABCDEFGHIJ"[i], fill=p.muted, font=theme.font(9))
            self.create_text(LABEL / 2, LABEL + i * CELL + CELL / 2, text=str(i + 1), fill=p.muted, font=theme.font(9))
        sunk = self.board.sunk_cells()
        for x in range(SIZE):
            for y in range(SIZE):
                cell = (x, y)
                x0, y0, x1, y1 = self._rect(cell)
                fill = p.field
                if self.own and self.board.ship_at(cell) is not None:
                    fill = p.accent  # own ships in the selection color
                elif cell == self._hover and self.interactive and cell not in self.board.shots:
                    fill = p.select
                theme._round_rect(self, x0, y0, x1, y1, 7, fill=fill, outline=p.border if fill == p.field else fill)
                if cell in self.board.shots:
                    self._mark(cell, self.board.shots[cell], cell in sunk)

    def _mark(self, cell: Cell, hit: bool, sunk: bool) -> None:
        p = theme.P
        x0, y0, x1, y1 = self._rect(cell, 11)
        if not hit:
            self.create_oval(x0 + 3, y0 + 3, x1 - 3, y1 - 3, fill=p.muted, outline="")
            return
        self.create_oval(x0 - 2, y0 - 2, x1 + 2, y1 + 2, fill=p.danger, outline="")
        if sunk:  # outline sunk ships as well
            ox0, oy0, ox1, oy1 = self._rect(cell, 3)
            self.create_rectangle(ox0, oy0, ox1, oy1, outline=p.danger, width=2)


class BattleshipWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg=theme.P.bg)
        self.title(tr("game.title"))
        self.resizable(False, False)
        self._game = Game()
        self._turn = "player"
        self._job: str | None = None
        self._build()
        theme.apply_window_chrome(self)

    def _build(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        body = tk.Frame(self, bg=theme.P.bg)
        body.pack(padx=28, pady=24)

        boards = tk.Frame(body, bg=theme.P.bg)
        boards.pack()
        self._grids: list[_Grid] = []
        for column, (key, board, own) in enumerate(
            (("game.you", self._game.player, True), ("game.enemy", self._game.enemy, False))
        ):
            holder = tk.Frame(boards, bg=theme.P.bg)
            holder.grid(row=0, column=column, padx=12)
            label(holder, tr(key), size=11, weight="bold").pack(anchor="w", pady=(0, 8), padx=6)
            box = RoundedBox(holder, radius=22)
            box.pack()
            grid = _Grid(box.body, board, own, on_click=None if own else self._player_shot)
            grid.pack()
            self._grids.append(grid)

        self._status = tk.Label(
            body, bg=theme.P.bg, fg=theme.P.text, font=theme.font(11), justify="center", wraplength=640, height=2
        )
        self._status.pack(pady=(18, 10))
        self._buttons = tk.Frame(body, bg=theme.P.bg)
        self._buttons.pack()
        self._show_setup()

    def _show_setup(self) -> None:
        for child in self._buttons.winfo_children():
            child.destroy()
        Button(self._buttons, tr("game.shuffle"), self._shuffle).pack(side="left", padx=6)
        Button(self._buttons, tr("game.start"), self._start, variant="primary", min_width=110).pack(side="left", padx=6)
        self._status.configure(text=tr("game.status.setup"))
        self._grids[1].interactive = False

    def _show_playing(self) -> None:
        for child in self._buttons.winfo_children():
            child.destroy()
        Button(self._buttons, tr("game.new"), self._new_game).pack()

    def _redraw(self) -> None:
        for grid in self._grids:
            grid.redraw()

    def _shuffle(self) -> None:
        self._game.shuffle_fleet()
        self._redraw()

    def _new_game(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        self._game, self._turn = Game(), "player"
        self._build()

    def _start(self) -> None:
        self._game.start()
        self._turn = "player"
        self._grids[1].interactive = True
        self._show_playing()
        self._status.configure(text=tr("game.status.you"))

    @staticmethod
    def _outcome(prefix: str, result: ShotResult) -> str:
        return tr(f"{prefix}.sunk" if result.sunk else f"{prefix}.hit" if result.hit else f"{prefix}.miss")

    def _player_shot(self, cell: Cell) -> None:
        if self._game.phase != "playing" or self._turn != "player" or cell in self._game.enemy.shots:
            return
        result = self._game.player_fire(cell)
        self._redraw()
        message = self._outcome("game.you", result)
        if self._game.phase == "over":
            self._finish(message)
            return
        self._turn = "bot"
        self._status.configure(text=f"{message}\n{tr('game.status.bot')}")
        self._job = self.after(BOT_DELAY_MS, self._bot_shot)

    def _bot_shot(self) -> None:
        self._job = None
        if self._game.phase != "playing":
            return
        _cell, result = self._game.bot_fire()
        self._redraw()
        message = self._outcome("game.bot", result)
        if self._game.phase == "over":
            self._finish(message)
            return
        self._turn = "player"
        self._status.configure(text=f"{message}\n{tr('game.status.you')}")

    def _finish(self, message: str) -> None:
        self._grids[1].interactive = False
        verdict = tr("game.win") if self._game.winner == "player" else tr("game.lose")
        self._status.configure(text=f"{message}\n{verdict}")
