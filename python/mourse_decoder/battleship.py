"""Battleship (easter egg): game logic and a bot, no tkinter.

Classic rules: 10x10, fleet 5-4-3-3-2, one shot per turn. The bot hunts on a
checkerboard, follows hits to the neighbours and then along the line until the
ship sinks. It never looks at the player's fleet.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

SIZE = 10
FLEET: tuple[int, ...] = (5, 4, 3, 3, 2)

Cell = tuple[int, int]  # (column x, row y)


def in_board(cell: Cell) -> bool:
    return 0 <= cell[0] < SIZE and 0 <= cell[1] < SIZE


def neighbours(cell: Cell) -> list[Cell]:
    x, y = cell
    return [c for c in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)) if in_board(c)]


@dataclass
class Ship:
    cells: frozenset[Cell]
    hits: set[Cell] = field(default_factory=set)

    @property
    def sunk(self) -> bool:
        return self.hits == set(self.cells)


@dataclass(frozen=True)
class ShotResult:
    hit: bool
    sunk: Ship | None = None  # set when this shot sank the ship
    already: bool = False     # the cell was shot before, so it doesn't count


class Board:
    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self.ships: list[Ship] = []
        self.shots: dict[Cell, bool] = {}  # shot cell -> hit?
        self.shuffle()

    def shuffle(self) -> None:
        """Place the fleet randomly; ships never touch, not even diagonally."""
        self.ships, self.shots = [], {}
        occupied: set[Cell] = set()
        for length in FLEET:
            while True:
                horizontal = self._rng.random() < 0.5
                x = self._rng.randrange(SIZE - (length - 1 if horizontal else 0))
                y = self._rng.randrange(SIZE - (0 if horizontal else length - 1))
                cells = [(x + i, y) if horizontal else (x, y + i) for i in range(length)]
                halo = {(cx + dx, cy + dy) for cx, cy in cells for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
                if not halo & occupied:
                    occupied |= set(cells)
                    self.ships.append(Ship(frozenset(cells)))
                    break

    def ship_at(self, cell: Cell) -> Ship | None:
        return next((s for s in self.ships if cell in s.cells), None)

    def fire(self, cell: Cell) -> ShotResult:
        if not in_board(cell):
            raise ValueError(f"Feld außerhalb des Spielfelds: {cell}")
        if cell in self.shots:
            return ShotResult(hit=self.shots[cell], already=True)
        ship = self.ship_at(cell)
        self.shots[cell] = ship is not None
        if ship is None:
            return ShotResult(hit=False)
        ship.hits.add(cell)
        return ShotResult(hit=True, sunk=ship if ship.sunk else None)

    @property
    def all_sunk(self) -> bool:
        return all(s.sunk for s in self.ships)

    def sunk_cells(self) -> set[Cell]:
        return {c for s in self.ships if s.sunk for c in s.cells}


class Bot:
    """Hunt on a checkerboard, chase hits, then go back to hunting after a kill."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self.tried: set[Cell] = set()
        self._open_hits: list[Cell] = []  # hits on ships that haven't sunk yet

    def next_shot(self) -> Cell:
        target = self._targets()
        if target:
            cell = self._rng.choice(target)
        else:
            free = [(x, y) for x in range(SIZE) for y in range(SIZE) if (x, y) not in self.tried]
            if not free:
                raise RuntimeError("Alle Felder wurden bereits beschossen")
            parity = [c for c in free if (c[0] + c[1]) % 2 == 0]
            cell = self._rng.choice(parity or free)
        self.tried.add(cell)
        return cell

    def report(self, cell: Cell, result: ShotResult) -> None:
        if not result.hit:
            return
        self._open_hits.append(cell)
        if result.sunk is not None:
            self._open_hits = [c for c in self._open_hits if c not in result.sunk.cells]

    def _targets(self) -> list[Cell]:
        hits = self._open_hits
        if not hits:
            return []
        candidates: list[Cell] = []
        if len(hits) >= 2:
            xs, ys = {c[0] for c in hits}, {c[1] for c in hits}
            if len(ys) == 1:  # horizontal: extend the line at both ends
                y = next(iter(ys))
                candidates = [(min(xs) - 1, y), (max(xs) + 1, y)]
            elif len(xs) == 1:
                x = next(iter(xs))
                candidates = [(x, min(ys) - 1), (x, max(ys) + 1)]
        free = [c for c in candidates if in_board(c) and c not in self.tried]
        if free:
            return free
        # hits from different ships, or the line is blocked: try all neighbours
        around = {n for h in hits for n in neighbours(h)}
        return sorted(c for c in around if c not in self.tried)


class Game:
    """Player vs bot. Phases: setup, playing, over."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self.player = Board(self._rng)
        self.enemy = Board(self._rng)
        self.bot = Bot(self._rng)
        self.phase = "setup"
        self.winner: str | None = None

    def shuffle_fleet(self) -> None:
        if self.phase == "setup":
            self.player.shuffle()

    def start(self) -> None:
        self.phase = "playing"

    def player_fire(self, cell: Cell) -> ShotResult:
        if self.phase != "playing":
            raise RuntimeError("Das Spiel läuft nicht")
        result = self.enemy.fire(cell)
        if not result.already and self.enemy.all_sunk:
            self.phase, self.winner = "over", "player"
        return result

    def bot_fire(self) -> tuple[Cell, ShotResult]:
        if self.phase != "playing":
            raise RuntimeError("Das Spiel läuft nicht")
        cell = self.bot.next_shot()
        result = self.player.fire(cell)
        self.bot.report(cell, result)
        if self.player.all_sunk:
            self.phase, self.winner = "over", "bot"
        return cell, result
