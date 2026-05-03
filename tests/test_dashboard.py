"""Tests for the M15 dashboard (Phase 2c).

Covers:
  - Fail-closed posture on external binding without password
  - Password auth middleware when bound externally
  - Live / Tuning / Logs / Health / Eval read endpoints
  - Tuning writes persist, audit log records every change, reject invalid
    values, and the mood-delta confirmation gate
  - Journal tag endpoint + immersion-break counts
  - Manual pause records a bridge cooldown via the safety layer

Runs against the FastAPI app with starlette's TestClient; no network
involved.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.dashboard.audit import DashboardAuditLog
from src.dashboard.config import DashboardConfig, DashboardConfigError
from src.dashboard.journal import M15Journal, TAG_HIT, TAG_IMMERSION_BREAK
from src.dashboard.server import build_app
from src.dashboard.snapshot import live_snapshot
from src.dashboard.tuning import (
    update_hedge_frequency,
    update_mood_baseline,
    update_never_uses,
)
from src.safety import SafetyLayer
from src.safety.config import (
    HealthMonitorConfig,
    PIIScrubberConfig,
    RealityAnchorsConfig,
    SafetyConfig,
)


ROOT = Path(__file__).resolve().parents[1]
RENEE_YAML = ROOT / "configs" / "renee.yaml"


@pytest.fixture
def env(tmp_path: Path) -> dict:
    """Build an isolated dashboard env under tmp_path with a copy of the
    shipped renee.yaml and a minimal safety.yaml. Ensures tests never
    mutate the real configs."""
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "configs"
    state_dir.mkdir()
    config_dir.mkdir()
    # Copy renee.yaml for real baseline values.
    (config_dir / "renee.yaml").write_text(
        RENEE_YAML.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (config_dir / "safety.yaml").write_text(
        yaml.safe_dump(
            {
                "reality_anchors": {
                    "enabled": True,
                    "rate_denominator": 50,
                    "min_turn_gap": 8,
                    "phrases": ["phrase"],
                    "suppress_when_any_of": [],
                },
                "health_monitor": {
                    "enabled": True,
                    "daily_minutes_soft_threshold": 240,
                    "sustained_days_soft": 14,
                    "daily_minutes_stronger_threshold": 360,
                    "sustained_days_stronger": 28,
                    "repeat_cooldown_days": 14,
                    "daily_cap_minutes": 120,
                    "post_cap_cooldown_minutes": 60,
                    "cap_disconnect_message": "That's the day. I'll be here tomorrow.",
                },
                "pii_scrubber": {"enabled": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cfg = DashboardConfig(
        bind_host="127.0.0.1",
        port=7860,
        password="",
        state_dir=str(state_dir),
        config_dir=str(config_dir),
        persona="renee",
    )
    return {"cfg": cfg, "state_dir": state_dir, "config_dir": config_dir}


@pytest.fixture
def client(env: dict) -> TestClient:
    app = build_app(env["cfg"])
    return TestClient(app)


# ---------------------------------------------------------------------------
# config posture
# ---------------------------------------------------------------------------


def test_external_bind_requires_password():
    cfg = DashboardConfig(bind_host="0.0.0.0", port=7860, password="")
    with pytest.raises(DashboardConfigError):
        cfg.validate()


def test_loopback_bind_accepts_empty_password():
    DashboardConfig(bind_host="127.0.0.1", port=7860, password="").validate()
    DashboardConfig(bind_host="localhost", port=7860, password="").validate()


def test_external_bind_with_password_accepts():
    DashboardConfig(bind_host="0.0.0.0", port=7860, password="secret").validate()


def test_password_middleware_blocks_when_external(tmp_path: Path):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "configs"
    state_dir.mkdir()
    config_dir.mkdir()
    (config_dir / "renee.yaml").write_text(RENEE_YAML.read_text(encoding="utf-8"), encoding="utf-8")
    (config_dir / "safety.yaml").write_text("health_monitor: {enabled: false}", encoding="utf-8")
    cfg = DashboardConfig(
        bind_host="192.168.1.10",
        port=7860,
        password="s3cret",
        state_dir=str(state_dir),
        config_dir=str(config_dir),
        persona="renee",
    )
    app = build_app(cfg)
    c = TestClient(app)
    r = c.get("/api/ping")
    assert r.status_code == 401
    r2 = c.get("/api/ping", headers={"X-Dashboard-Password": "s3cret"})
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# core endpoints
# ---------------------------------------------------------------------------


def test_index_serves_html(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "Renée M15" in r.text
    assert "tab-live" in r.text


def test_ping(client: TestClient):
    r = client.get("/api/ping")
    assert r.status_code == 200
    assert r.json()["persona"] == "renee"


def test_cost_endpoint_handles_pod_unreachable(client: TestClient, monkeypatch):
    """When PodManager.status() raises (no API key, network down), the
    endpoint returns ok:false instead of 500-ing the dashboard."""
    from src.client import pod_manager
    def boom(self):
        raise RuntimeError("no API key")
    monkeypatch.setattr(pod_manager.PodManager, "status", boom)
    r = client.get("/api/cost")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "error" in body


def test_cost_endpoint_returns_estimate(client: TestClient, monkeypatch):
    """When status() returns a running pod, the estimate is present and
    rate selection picks a value from the known table."""
    from src.client import pod_manager
    def stub(self):
        return {
            "id": "pod-x", "status": "RUNNING", "public_ip": "1.2.3.4",
            "uptime_seconds": 3600,  # 1h
            "gpu_type": "NVIDIA A100 SXM",
        }
    monkeypatch.setattr(pod_manager.PodManager, "status", stub)
    r = client.get("/api/cost")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "RUNNING"
    assert body["uptime_minutes"] == 60.0
    # A100 SXM = $1.50/hr, 1h elapsed
    assert body["hourly_usd"] == 1.50
    assert abs(body["session_usd"] - 1.50) < 0.01


def test_cost_history_endpoint_aggregates_ledger(client: TestClient, tmp_path: Path, monkeypatch):
    """Insert two down events at known timestamps, hit /api/cost/history,
    verify it sums month-to-date correctly."""
    from src.client import cost_ledger
    db = tmp_path / "ledger.db"
    monkeypatch.setattr(cost_ledger, "DEFAULT_LEDGER_PATH", db)
    cost_ledger.record_down(
        pod_id="p1", minutes=60, hourly_usd=1.50, gpu_type="A100", db_path=db,
    )
    r = client.get("/api/cost/history")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # 1h × $1.50 = $1.50
    assert body["this_month_usd"] == 1.50
    assert body["samples"] == 1
    assert body["recent"][0]["pod_id"] == "p1"


def test_cost_history_endpoint_handles_missing_ledger(client: TestClient, tmp_path: Path, monkeypatch):
    """Empty ledger returns zeros without crashing."""
    from src.client import cost_ledger
    db = tmp_path / "fresh.db"
    monkeypatch.setattr(cost_ledger, "DEFAULT_LEDGER_PATH", db)
    r = client.get("/api/cost/history")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["this_month_usd"] == 0
    assert body["recent"] == []


def test_live_snapshot_structure(client: TestClient):
    r = client.get("/api/live/snapshot")
    assert r.status_code == 200
    data = r.json()
    assert set(data["mood"].keys()) >= {"current", "baseline", "bad_day"}
    assert len(data["mood"]["current"]) == 6
    assert data["bridge"]["allowed"] is True
    assert "daily_cap_minutes" in data["health"]


def test_tuning_state_returns_current_values(client: TestClient, env: dict):
    r = client.get("/api/tuning/state")
    assert r.status_code == 200
    data = r.json()
    assert "baseline_mood" in data["persona"]
    assert "health_monitor" in data["safety"]
    assert data["mood_axis_max_delta"] == 0.2


# ---------------------------------------------------------------------------
# tuning writes
# ---------------------------------------------------------------------------


def test_mood_baseline_small_delta_persists(client: TestClient, env: dict):
    # Start with a known value by reading once.
    cur = client.get("/api/tuning/state").json()["persona"]["baseline_mood"].get(
        "warmth", 0.75
    )
    new = max(0.0, min(1.0, float(cur) + 0.05))
    r = client.post(
        "/api/tuning/mood_baseline",
        json={"axis": "warmth", "value": new, "confirmed": False},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    # Re-read; should reflect the write.
    after = client.get("/api/tuning/state").json()["persona"]["baseline_mood"]["warmth"]
    assert abs(after - new) < 1e-6


def test_mood_baseline_large_delta_requires_confirm(client: TestClient):
    cur = client.get("/api/tuning/state").json()["persona"]["baseline_mood"].get(
        "warmth", 0.75
    )
    target = 0.05 if cur > 0.3 else 0.95
    r = client.post(
        "/api/tuning/mood_baseline",
        json={"axis": "warmth", "value": target, "confirmed": False},
    )
    assert r.status_code == 409
    r2 = client.post(
        "/api/tuning/mood_baseline",
        json={"axis": "warmth", "value": target, "confirmed": True},
    )
    assert r2.status_code == 200


def test_mood_baseline_rejects_unknown_axis(client: TestClient):
    r = client.post(
        "/api/tuning/mood_baseline",
        json={"axis": "telepathy", "value": 0.5, "confirmed": True},
    )
    assert r.status_code == 500 or r.status_code >= 400


def test_mood_baseline_rejects_out_of_range(client: TestClient):
    r = client.post(
        "/api/tuning/mood_baseline",
        json={"axis": "warmth", "value": 2.0, "confirmed": True},
    )
    assert r.status_code == 422


def test_hedge_frequency_persists_and_audits(client: TestClient, env: dict):
    r = client.post("/api/tuning/hedge_frequency", json={"value": 0.42})
    assert r.status_code == 200
    after = client.get("/api/tuning/state").json()["persona"]["speech_patterns"][
        "hedge_frequency"
    ]
    assert after == 0.42
    recent = client.get("/api/audit/recent").json()
    fields = [e["field"] for e in recent["entries"]]
    assert "persona.speech_patterns.hedge_frequency" in fields


def test_never_use_dedupes(client: TestClient):
    r = client.post(
        "/api/tuning/never_use",
        json={"phrases": ["as an AI", "as an AI", "you're right"]},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["new_value"] == ["as an AI", "you're right"]


def test_safety_caps_requires_confirm_token(client: TestClient):
    r = client.post(
        "/api/tuning/safety_caps",
        json={"daily_cap_minutes": 90},
    )
    assert r.status_code == 409
    r2 = client.post(
        "/api/tuning/safety_caps",
        json={"daily_cap_minutes": 90, "confirm": "confirm"},
    )
    assert r2.status_code == 200
    after = client.get("/api/tuning/state").json()["safety"]["health_monitor"][
        "daily_cap_minutes"
    ]
    assert after == 90


# ---------------------------------------------------------------------------
# logs & journal
# ---------------------------------------------------------------------------


def test_logs_conversation_empty_day(client: TestClient):
    r = client.get("/api/logs/conversation?day=2026-04-19")
    assert r.status_code == 200
    data = r.json()
    assert data["exists"] is False
    assert data["lines"] == []


def test_logs_conversation_reads_dated_file(client: TestClient, env: dict):
    day = "2026-04-19"
    log_path = env["state_dir"] / "logs" / "conversations" / f"{day}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("[09:00:00] PAUL: hey\n[09:00:02] RENEE: hey back\n", encoding="utf-8")
    r = client.get(f"/api/logs/conversation?day={day}")
    data = r.json()
    assert data["exists"] is True
    assert "PAUL: hey" in data["lines"][0]


def test_logs_tag_immersion_break_persists(client: TestClient):
    r = client.post(
        "/api/logs/tag",
        json={"tag": TAG_IMMERSION_BREAK, "day": "2026-04-19", "note": "ip_reminder leak"},
    )
    assert r.status_code == 200
    j = client.get("/api/logs/journal?day=2026-04-19").json()
    assert len(j["entries"]) == 1
    assert j["entries"][0]["tag"] == TAG_IMMERSION_BREAK
    assert "ip_reminder" in j["entries"][0]["note"]


def test_logs_tag_unknown_tag_rejected(client: TestClient):
    r = client.post(
        "/api/logs/tag",
        json={"tag": "not_a_real_tag", "day": "2026-04-19", "note": ""},
    )
    assert r.status_code == 422


def test_logs_export_txt(client: TestClient, env: dict):
    day = "2026-04-19"
    log_path = env["state_dir"] / "logs" / "conversations" / f"{day}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("[09:00:00] PAUL: one\n[09:00:01] RENEE: two\n", encoding="utf-8")
    r = client.get(f"/api/logs/export?day={day}&fmt=txt")
    assert r.status_code == 200
    assert "PAUL: one" in r.text


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_summary_structure(client: TestClient):
    r = client.get("/api/health/summary")
    assert r.status_code == 200
    data = r.json()
    assert "daily_minutes" in data
    assert "rolling_30_day" in data
    assert len(data["rolling_30_day"]) == 30


def test_manual_pause_writes_cooldown_via_safety_layer(env: dict, tmp_path: Path):
    """When a safety layer is attached, the pause endpoint writes a
    bridge_cooldowns row; subsequent bridge_allowed_now() returns False."""
    safety = SafetyLayer(
        SafetyConfig(
            reality_anchors=RealityAnchorsConfig(enabled=False),
            health_monitor=HealthMonitorConfig(
                enabled=True, daily_cap_minutes=120, post_cap_cooldown_minutes=60,
            ),
            pii_scrubber=PIIScrubberConfig(enabled=False),
        ),
        env["state_dir"],
        rng=random.Random(0),
    )
    # Reuse the default (env) state_dir so the dashboard and safety see the
    # same SQLite file.
    app = build_app(env["cfg"], safety_layer=safety)
    c = TestClient(app)

    assert safety.bridge_allowed_now() is True
    r = c.post("/api/health/pause", json={"hours": 1, "reason": "testing", "confirm": "confirm"})
    assert r.status_code == 200
    assert safety.bridge_allowed_now() is False


def test_manual_pause_requires_confirm(client: TestClient):
    r = client.post("/api/health/pause", json={"hours": 24, "reason": ""})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# eval + audit
# ---------------------------------------------------------------------------


def test_eval_summary_no_turns(client: TestClient):
    r = client.get("/api/eval/summary")
    assert r.status_code == 200
    data = r.json()
    assert data["turns"] == 0


def test_eval_dashboard_path_reports_presence(client: TestClient, env: dict):
    r = client.get("/api/eval/dashboard_path")
    assert r.status_code == 200
    assert r.json()["exists"] is False
    (env["state_dir"] / "eval_dashboard.html").write_text("<html></html>", encoding="utf-8")
    r2 = client.get("/api/eval/dashboard_path")
    assert r2.json()["exists"] is True


def test_audit_log_records_every_write(client: TestClient):
    client.post("/api/tuning/hedge_frequency", json={"value": 0.33})
    client.post("/api/tuning/never_use", json={"phrases": ["nope"]})
    r = client.get("/api/audit/recent")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 2
    # Every entry carries a signed receipt id.
    for e in data["entries"][:2]:
        assert e["receipt_id"]


def test_audit_log_preserves_old_and_new(tmp_path: Path):
    audit = DashboardAuditLog(tmp_path / "actions.db")
    audit.record(
        field="test.field",
        old_value={"a": 1},
        new_value={"a": 2},
        confirmed=False,
        actor="pj",
        receipt_id="r1",
    )
    rows = audit.recent(limit=5)
    assert len(rows) == 1
    assert json.loads(rows[0].old_value) == {"a": 1}
    assert json.loads(rows[0].new_value) == {"a": 2}


def test_live_snapshot_cold_read_is_safe(tmp_path: Path):
    """The Live snapshot must not crash when no orchestrator run has ever
    produced metrics or telemetry on this state dir."""
    (tmp_path / "state").mkdir()
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "renee.yaml").write_text(
        RENEE_YAML.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "configs" / "safety.yaml").write_text(
        yaml.safe_dump({"health_monitor": {"enabled": True}}), encoding="utf-8",
    )
    data = live_snapshot(
        state_dir=tmp_path / "state",
        config_dir=tmp_path / "configs",
        persona="renee",
    )
    assert data["latency"]["count"] == 0
    assert data["anchor"]["count"] == 0
    assert data["bridge"]["allowed"] is True
