"""Tests for the easter egg: SOS detection and the battleship logic with its bot."""

from __future__ import annotations

import random

import pytest
from mourse_decoder.battleship import FLEET, SIZE, Board, Bot, Game, neighbours
from mourse_decoder.easter_egg import SequenceDetector


def tap(detector, pattern, start=0.0, dot=0.1, dash=0.5, gap=0.2):
    """Replay clicks; returns whether SOS was recognized after each click."""
    t, hits = start, []
    for symbol in pattern:
        detector.press(t)
        t += dot if symbol == "." else dash
        hits.append(detector.release(t))
        t += gap
    return hits, t


def test_sos_is_detected_after_the_last_click():
    hits, _ = tap(SequenceDetector(), "...---...")
    assert hits == [False] * 8 + [True]


def test_other_sequences_do_not_trigger():
    for pattern in ("...---..", "---...---", "........."):
        hits, _ = tap(SequenceDetector(), pattern)
        assert not any(hits), pattern


def test_noise_before_sos_is_ignored():
    hits, _ = tap(SequenceDetector(), "-.-...---...")
    assert hits[-1] is True


def test_long_pause_restarts_the_input():
    detector = SequenceDetector()
    _, t = tap(detector, "...---.")
    hits, _ = tap(detector, "..", start=t + 3.0)  # too late, the start no longer counts
    assert not any(hits)


def test_detector_can_trigger_again_and_release_without_press_is_safe():
    detector = SequenceDetector()
    assert detector.release(1.0) is False
    _, t = tap(detector, "...---...")
    hits, _ = tap(detector, "...---...", start=t)
    assert hits[-1] is True


@pytest.mark.parametrize("seed", range(30))
def test_random_fleet_is_valid(seed):
    board = Board(random.Random(seed))
    assert sorted(len(s.cells) for s in board.ships) == sorted(FLEET)
    cells = [c for s in board.ships for c in s.cells]
    assert len(cells) == len(set(cells)) == sum(FLEET)
    assert all(0 <= x < SIZE and 0 <= y < SIZE for x, y in cells)
    # ships never touch, not even diagonally
    for ship in board.ships:
        for x, y in ship.cells:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    other = board.ship_at((x + dx, y + dy))
                    assert other is None or other is ship


def test_fire_results_and_sinking():
    board = Board(random.Random(1))
    ship = board.ships[-1]  # the smallest (length 2)
    first, second = sorted(ship.cells)
    assert board.fire(first).hit and board.fire(first).already
    result = board.fire(second)
    assert result.hit and result.sunk is ship
    water = next((x, y) for x in range(SIZE) for y in range(SIZE) if board.ship_at((x, y)) is None)
    assert board.fire(water).hit is False
    with pytest.raises(ValueError):
        board.fire((SIZE, 0))


def test_all_sunk_after_hitting_every_cell():
    board = Board(random.Random(2))
    for ship in board.ships:
        for cell in ship.cells:
            board.fire(cell)
    assert board.all_sunk


@pytest.mark.parametrize("seed", range(20))
def test_bot_never_repeats_and_finishes_the_game(seed):
    rng = random.Random(seed)
    board, bot = Board(rng), Bot(rng)
    shots = []
    while not board.all_sunk:
        cell = bot.next_shot()
        assert cell not in shots
        shots.append(cell)
        bot.report(cell, board.fire(cell))
        assert len(shots) <= SIZE * SIZE
    assert len(shots) < 100


def test_bot_is_clearly_better_than_random_on_average():
    # a random shooter needs ~95 shots on average
    totals = []
    for seed in range(40):
        rng = random.Random(seed)
        board, bot = Board(rng), Bot(rng)
        count = 0
        while not board.all_sunk:
            cell = bot.next_shot()
            bot.report(cell, board.fire(cell))
            count += 1
        totals.append(count)
    assert sum(totals) / len(totals) < 75


def test_bot_follows_up_on_a_hit():
    rng = random.Random(3)
    board, bot = Board(rng), Bot(rng)
    ship = board.ships[0]
    cell = sorted(ship.cells)[0]
    bot.tried.add(cell)
    bot.report(cell, board.fire(cell))
    assert bot.next_shot() in neighbours(cell)


def test_game_flow_player_wins_when_all_enemy_ships_are_sunk():
    game = Game(random.Random(5))
    with pytest.raises(RuntimeError):
        game.player_fire((0, 0))  # still in setup
    game.start()
    for ship in game.enemy.ships:
        for cell in ship.cells:
            game.player_fire(cell)
    assert game.phase == "over" and game.winner == "player"
    with pytest.raises(RuntimeError):
        game.bot_fire()


def test_game_flow_bot_wins_eventually_against_passive_player():
    game = Game(random.Random(6))
    game.start()
    for _ in range(SIZE * SIZE):
        game.bot_fire()
        if game.phase == "over":
            break
    assert game.winner == "bot"


def test_shuffle_only_in_setup():
    game = Game(random.Random(7))
    before = {s.cells for s in game.player.ships}
    game.shuffle_fleet()
    assert {s.cells for s in game.player.ships} != before
    game.start()
    after = {s.cells for s in game.player.ships}
    game.shuffle_fleet()
    assert {s.cells for s in game.player.ships} == after
