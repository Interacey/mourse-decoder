"""Scroller: speed levels, stop conditions and thread handling (fake wheel, no real input)."""

from __future__ import annotations

import threading
import time

import pytest
from mourse_decoder.scroller import DOWN, MAX_SCROLL_S, NOTCHES_PER_SECOND, UP, Scroller, ScrollState, interval_s


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def count_notches(state: ScrollState, seconds: float, step: float = 0.01) -> int:
    total = 0
    for i in range(int(round(seconds / step))):
        total += abs(state.due(i * step))
    return total


def make_scroller(clock, should_stop, wheel=None, on_error=None):
    sent: list[int] = []
    errors: list[str] = []
    s = Scroller(
        wheel=wheel or sent.append,
        should_stop=should_stop,
        on_error=on_error or errors.append,
        clock=clock,
        wait=lambda seconds: clock.advance(seconds or 0.001),
        spawn=lambda body: None,
    )
    return s, sent, errors


def test_first_request_notches_at_once():
    state = ScrollState()
    state.request(DOWN, 0.0)
    assert state.due(0.0) == 1
    other = ScrollState()
    other.request(UP, 0.0)
    assert other.due(0.0) == -1


def test_levels_and_intervals():
    state = ScrollState()
    for k in range(1, 10):
        state.request(DOWN, 0.0)
        assert state.level == min(k, len(NOTCHES_PER_SECOND))
    values = [interval_s(n) for n in range(1, len(NOTCHES_PER_SECOND) + 1)]
    assert all(a > b for a, b in zip(values, values[1:], strict=False))
    assert interval_s(99) == interval_s(6) == 1 / 25
    with pytest.raises(ValueError):
        interval_s(0)


def test_notches_per_second():
    state = ScrollState()
    state.request(DOWN, 0.0)
    assert count_notches(state, 1.0) == 3
    fast = ScrollState()
    for _ in range(6):
        fast.request(DOWN, 0.0)
    assert abs(count_notches(fast, 1.0) - 25) <= 1


def test_speed_up_applies_at_once():
    state = ScrollState()
    state.request(DOWN, 0.0)
    assert state.due(0.0) == 1
    before = state.next_due()
    state.request(DOWN, 0.01)
    assert state.next_due() < before


def test_reverse_starts_at_level_one_with_a_notch():
    state = ScrollState()
    for _ in range(4):
        state.request(DOWN, 0.0)
    state.due(0.0)
    state.request(UP, 0.1)
    assert (state.direction, state.level) == (UP, 1)
    assert state.started_at == 0.0
    assert state.due(0.1) == -1


def test_stop_resets():
    state = ScrollState()
    for _ in range(3):
        state.request(DOWN, 0.0)
    state.stop()
    assert (state.direction, state.level, state.started_at, state.next_due()) == (0, 0, None, None)
    assert state.due(5.0) == 0
    state.request(DOWN, 5.0)
    assert state.level == 1


def test_clock_jump_gives_no_burst():
    state = ScrollState()
    state.request(DOWN, 0.0)
    assert state.due(0.0) == 1
    assert state.due(2.0) == 1
    assert state.due(2.0) == 0
    assert state.next_due() == pytest.approx(2.0 + interval_s(1))


def test_stop_when_idle_and_bad_direction():
    state = ScrollState()
    state.stop()
    assert state.direction == 0
    with pytest.raises(ValueError):
        state.request(0, 0.0)


def test_run_stops_on_reason():
    clock = FakeClock()
    calls = []

    def should_stop():
        calls.append(1)
        return "Esc" if len(calls) == 3 else None

    s, sent, errors = make_scroller(clock, should_stop)
    for _ in range(6):  # level 6: the second notch is 0.04 s after the first
        s.request(DOWN)
    s.run()
    assert sent == [1, 1]
    assert not s.running
    assert errors == []


def test_wheel_error_stops_and_reports():
    clock = FakeClock()

    def wheel(n):
        raise OSError("kaputt")

    s, _sent, errors = make_scroller(clock, lambda: None, wheel=wheel)
    s.request(DOWN)
    s.run()
    assert not s.running
    assert len(errors) == 1 and errors[0].startswith("Scrollen gestoppt")


def test_max_duration_stops_with_notice():
    clock = FakeClock()
    s, sent, errors = make_scroller(clock, lambda: None)
    s.request(DOWN)
    s.run()
    assert not s.running
    assert errors == ["Scrollen nach 10 Minuten automatisch beendet."]
    assert clock.now >= MAX_SCROLL_S
    assert sent


def test_stop_inside_wheel_ends_loop():
    clock = FakeClock()
    holder = []

    def wheel(n):
        sent.append(n)
        assert holder[0].stop() is True

    s, sent, _errors = make_scroller(clock, lambda: None, wheel=wheel)
    holder.append(s)
    s.request(DOWN)
    s.run()
    assert sent == [1]
    assert not s.running


def test_reverse_while_running():
    clock = FakeClock()
    calls = []
    sent = []
    levels = []
    holder = []

    def wheel(n):
        sent.append(n)
        if len(sent) == 1:
            holder[0].request(UP)
        levels.append(holder[0].level)

    def should_stop():
        calls.append(1)
        return "Ende" if len(calls) > 30 else None

    s, _unused, _errors = make_scroller(clock, should_stop, wheel=wheel)
    holder.append(s)
    s.request(DOWN)
    s.run()
    assert sent[0] == 1 and sent[1:] and all(n == -1 for n in sent[1:])
    assert levels[0] == 1


def test_real_thread_scrolls_and_ends():
    sent = []
    threads = []

    def spawn(body):
        thread = threading.Thread(target=body, name="scroller", daemon=True)
        threads.append(thread)
        thread.start()

    s = Scroller(wheel=sent.append, should_stop=lambda: None, spawn=spawn)
    s.request(DOWN)
    deadline = time.monotonic() + 2
    while not sent and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sent
    assert s.stop() is True
    assert len(threads) == 1
    threads[0].join(timeout=2)
    assert not threads[0].is_alive()


def test_restart_before_thread_start_runs_only_the_newest_loop():
    clock = FakeClock()
    bodies = []
    sent = []
    calls = []

    def should_stop():
        calls.append(1)
        return "Ende" if len(calls) > 3 else None

    s = Scroller(
        wheel=sent.append,
        should_stop=should_stop,
        clock=clock,
        wait=lambda seconds: clock.advance(seconds or 0.001),
        spawn=bodies.append,
    )
    s.request(DOWN)
    s.stop()
    s.request(DOWN)
    assert len(bodies) == 2
    bodies[0]()
    assert sent == [] and calls == [] and s.running
    bodies[1]()
    assert sent and not s.running
