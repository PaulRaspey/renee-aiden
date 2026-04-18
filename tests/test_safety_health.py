"""Tests for src.safety.health_monitor."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.safety.config import HealthMonitorConfig
from src.safety.health_monitor import FLAG_SOFT, FLAG_STRONGER, HealthMonitor


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 4, 1, 9, 0, 0))


def test_record_turn_aggregates_daily_minutes(tmp_path: Path, clock: FakeClock):
    hm = HealthMonitor(tmp_path / "health.db", now_fn=clock)
    hm.record_turn(60_000)   # 1 min
    hm.record_turn(90_000)   # 1.5 min
    assert hm.daily_minutes() == pytest.approx(2.5, abs=1e-2)


def test_daily_summary_matches_partial_day(tmp_path: Path, clock: FakeClock):
    hm = HealthMonitor(tmp_path / "health.db", now_fn=clock)
    hm.record_turn(120_000)  # 2 min
    hm.record_turn(60_000)   # 1 min
    summary = hm.daily_summary()
    assert isinstance(summary, float)
    assert summary == pytest.approx(3.0, abs=1e-2)


def test_rolling_daily_minutes_spans_multiple_days(tmp_path: Path, clock: FakeClock):
    hm = HealthMonitor(tmp_path / "health.db", now_fn=clock)
    hm.record_turn(60_000)
    clock.advance(days=1)
    hm.record_turn(120_000)
    clock.advance(days=1)
    hm.record_turn(180_000)
    rolling = hm.rolling_daily_minutes(3)
    assert [d[1] for d in rolling] == [1.0, 2.0, 3.0]


def test_disabled_monitor_is_no_op(tmp_path: Path, clock: FakeClock):
    cfg = HealthMonitorConfig(enabled=False)
    hm = HealthMonitor(tmp_path / "health.db", cfg=cfg, now_fn=clock)
    hm.record_turn(60_000)
    # No rows written.
    assert hm.daily_minutes() == 0.0
    assert hm.check_flags() == []


def test_soft_flag_raised_when_threshold_sustained(tmp_path: Path, clock: FakeClock):
    cfg = HealthMonitorConfig(
        enabled=True,
        daily_minutes_soft_threshold=60,
        sustained_days_soft=3,
        daily_minutes_stronger_threshold=9999,
        sustained_days_stronger=30,
        repeat_cooldown_days=7,
    )
    hm = HealthMonitor(tmp_path / "health.db", cfg=cfg, now_fn=clock)
    for _ in range(3):
        hm.record_turn(60 * 60_000)  # 60 minutes
        clock.advance(days=1)
    flags = hm.check_flags()
    assert any(f.flag_type == FLAG_SOFT for f in flags)


def test_soft_flag_suppressed_when_not_sustained(tmp_path: Path, clock: FakeClock):
    cfg = HealthMonitorConfig(
        enabled=True,
        daily_minutes_soft_threshold=60,
        sustained_days_soft=3,
        daily_minutes_stronger_threshold=9999,
        sustained_days_stronger=30,
        repeat_cooldown_days=7,
    )
    hm = HealthMonitor(tmp_path / "health.db", cfg=cfg, now_fn=clock)
    hm.record_turn(60 * 60_000)
    clock.advance(days=1)
    hm.record_turn(60 * 60_000)
    clock.advance(days=1)
    hm.record_turn(10 * 60_000)  # dip below threshold
    flags = hm.check_flags()
    assert not any(f.flag_type == FLAG_SOFT for f in flags)


def test_soft_flag_respects_cooldown(tmp_path: Path, clock: FakeClock):
    cfg = HealthMonitorConfig(
        enabled=True,
        daily_minutes_soft_threshold=60,
        sustained_days_soft=3,
        daily_minutes_stronger_threshold=9999,
        sustained_days_stronger=30,
        repeat_cooldown_days=7,
    )
    hm = HealthMonitor(tmp_path / "health.db", cfg=cfg, now_fn=clock)
    for _ in range(3):
        hm.record_turn(60 * 60_000)
        clock.advance(days=1)
    first = hm.check_flags()
    assert first
    # Immediate re-check inside cooldown should not re-raise.
    second = hm.check_flags()
    assert not second
