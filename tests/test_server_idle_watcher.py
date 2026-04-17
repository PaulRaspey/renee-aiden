"""Tests for src.server.idle_watcher."""
from __future__ import annotations

import pytest

from src.server.idle_watcher import IdleWatcher, IdleWatcherStatus


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_rejects_nonpositive_timeout():
    with pytest.raises(ValueError):
        IdleWatcher(0, on_shutdown=lambda: None)
    with pytest.raises(ValueError):
        IdleWatcher(-5, on_shutdown=lambda: None)


def test_tick_does_not_fire_before_timeout():
    clk = FakeClock()
    called = {"n": 0}
    w = IdleWatcher(60, on_shutdown=lambda: called.__setitem__("n", called["n"] + 1), now_fn=clk)
    clk.advance(30)
    assert w.tick() is False
    assert called["n"] == 0


def test_tick_fires_after_timeout():
    clk = FakeClock()
    called = {"n": 0}
    w = IdleWatcher(60, on_shutdown=lambda: called.__setitem__("n", called["n"] + 1), now_fn=clk)
    clk.advance(61)
    assert w.tick() is True
    assert called["n"] == 1
    # Second tick without new activity should not fire again.
    clk.advance(60)
    assert w.tick() is False
    assert called["n"] == 1


def test_mark_activity_resets_timer():
    clk = FakeClock()
    called = {"n": 0}
    w = IdleWatcher(60, on_shutdown=lambda: called.__setitem__("n", called["n"] + 1), now_fn=clk)
    clk.advance(50)
    w.mark_activity()
    clk.advance(50)
    assert w.tick() is False
    assert called["n"] == 0
    clk.advance(11)  # now 61s since last activity
    assert w.tick() is True
    assert called["n"] == 1


def test_mark_activity_rearms_after_trigger():
    clk = FakeClock()
    called = {"n": 0}
    w = IdleWatcher(60, on_shutdown=lambda: called.__setitem__("n", called["n"] + 1), now_fn=clk)
    clk.advance(61)
    w.tick()   # fires
    assert called["n"] == 1
    w.mark_activity()
    assert w.triggered is False
    clk.advance(61)
    assert w.tick() is True
    assert called["n"] == 2


def test_shutdown_callback_exception_is_swallowed():
    clk = FakeClock()
    def raiser():
        raise RuntimeError("boom")
    w = IdleWatcher(10, on_shutdown=raiser, now_fn=clk)
    clk.advance(11)
    # Should return True and not raise.
    assert w.tick() is True
    assert w.triggered is True


def test_status_snapshot():
    clk = FakeClock()
    w = IdleWatcher(60, on_shutdown=lambda: None, now_fn=clk)
    clk.advance(25)
    snap = IdleWatcherStatus.from_watcher(w)
    assert snap.seconds_idle == 25.0
    assert snap.seconds_remaining == 35.0
    assert snap.triggered is False
