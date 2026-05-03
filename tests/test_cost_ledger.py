"""Tests for src.client.cost_ledger (#8)."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from src.client.cost_ledger import (
    buckets,
    list_events,
    record_down,
    record_up,
)


def _utc(year: int, month: int, day: int, hour: int = 12) -> _dt.datetime:
    return _dt.datetime(year, month, day, hour, tzinfo=_dt.timezone.utc)


def test_record_up_inserts_zero_cost_row(tmp_path: Path):
    db = tmp_path / "ledger.db"
    rid = record_up(pod_id="pod-x", gpu_type="A100", hourly_usd=1.50, db_path=db)
    assert rid > 0
    events = list_events(db_path=db)
    assert len(events) == 1
    e = events[0]
    assert e.pod_id == "pod-x"
    assert e.event == "up"
    assert e.cost_usd == 0
    assert e.minutes == 0


def test_record_down_computes_cost(tmp_path: Path):
    db = tmp_path / "ledger.db"
    record_up(pod_id="pod-x", gpu_type="A100", hourly_usd=1.50, db_path=db)
    record_down(pod_id="pod-x", minutes=120, hourly_usd=1.50, db_path=db)
    events = list_events(db_path=db)
    assert events[0].event == "down"
    # 2h × $1.50 = $3.00
    assert events[0].cost_usd == pytest.approx(3.00)


def test_buckets_aggregates_today_and_month(tmp_path: Path):
    db = tmp_path / "ledger.db"
    # Two down events today (different pods), one earlier this month
    record_down(
        pod_id="p1", minutes=60, hourly_usd=1.0, db_path=db,
        now=_utc(2026, 5, 3, 9),
    )
    record_down(
        pod_id="p2", minutes=30, hourly_usd=2.0, db_path=db,
        now=_utc(2026, 5, 3, 11),
    )
    record_down(
        pod_id="p3", minutes=120, hourly_usd=1.5, db_path=db,
        now=_utc(2026, 5, 1, 14),
    )
    # And one from a prior month — must NOT count
    record_down(
        pod_id="p4", minutes=600, hourly_usd=3.0, db_path=db,
        now=_utc(2026, 4, 30, 10),
    )
    b = buckets(monthly_budget_usd=10, db_path=db, now=_utc(2026, 5, 3, 12))
    # Today (May 3): $1.00 + $1.00 = $2.00
    assert b.today_usd == 2.00
    # This month (May): $2.00 today + $3.00 on May 1 = $5.00
    assert b.this_month_usd == 5.00
    # 60 + 30 + 120 = 210 min
    assert b.this_month_minutes == 210.0
    assert b.monthly_budget_usd == 10
    assert b.over_budget is False


def test_buckets_flags_over_budget(tmp_path: Path):
    db = tmp_path / "ledger.db"
    record_down(
        pod_id="p1", minutes=300, hourly_usd=2.0, db_path=db,
        now=_utc(2026, 5, 3, 9),
    )
    b = buckets(monthly_budget_usd=5, db_path=db, now=_utc(2026, 5, 3, 12))
    # 5h × $2 = $10 > $5 budget
    assert b.this_month_usd == 10.0
    assert b.over_budget is True


def test_buckets_with_no_budget_never_over(tmp_path: Path):
    db = tmp_path / "ledger.db"
    record_down(
        pod_id="p1", minutes=60, hourly_usd=10.0, db_path=db,
        now=_utc(2026, 5, 3, 9),
    )
    b = buckets(monthly_budget_usd=None, db_path=db, now=_utc(2026, 5, 3, 12))
    assert b.over_budget is False
    assert b.monthly_budget_usd is None


def test_buckets_empty_ledger_returns_zeros(tmp_path: Path):
    db = tmp_path / "ledger.db"
    b = buckets(monthly_budget_usd=100, db_path=db, now=_utc(2026, 5, 3, 12))
    assert b.today_usd == 0
    assert b.this_month_usd == 0
    assert b.this_month_minutes == 0
    assert b.samples == 0


def test_list_events_returns_newest_first(tmp_path: Path):
    db = tmp_path / "ledger.db"
    record_up(pod_id="p1", db_path=db)
    record_up(pod_id="p2", db_path=db)
    record_down(pod_id="p2", minutes=30, hourly_usd=1.0, db_path=db)
    events = list_events(db_path=db, limit=2)
    # Most recent first — that's the down for p2
    assert events[0].pod_id == "p2"
    assert events[0].event == "down"
    assert events[1].pod_id == "p2"
    assert events[1].event == "up"


# The dashboard /api/cost/history endpoint test lives in test_dashboard.py
# next to the other endpoint tests, since it shares the env+client fixtures.
