"""FastAPI app for the M15 observability and tuning console.

build_app(cfg, orchestrator=None, safety_layer=None) returns an ASGI app.
When orchestrator/safety_layer are passed in, mutating endpoints also
trigger a runtime reload so PJ doesn't have to restart the pod. When
they're absent (tests, cold-reads), the endpoints still work and simply
write-through to YAML.

Auth posture:
- Loopback binding (127.0.0.1, ::1, localhost) requires no password.
- Any other bind_host requires a non-empty password; requests must carry
  it in an `X-Dashboard-Password` header or `?password=` query arg.
  Constant-time equality compare.
"""
from __future__ import annotations

import hmac
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from ..capture import dashboard_sessions as sessions_mod
from ..capture.dashboard_sessions import PresenceScoreLockedError
from ..capture.session_recorder import default_sessions_root
from .agent import DashboardAgent
from .audit import DashboardAuditLog
from .config import DashboardConfig
from .journal import TAG_HIT, TAG_IMMERSION_BREAK, TAG_PAUSE, M15Journal
from .snapshot import health_snapshot, live_snapshot, logs_for_day
from . import tuning as tuning_mod


HEADER_AUTH = "x-dashboard-password"


# ---------------------------------------------------------------------------
# pydantic payloads
# ---------------------------------------------------------------------------


class MoodBaselinePayload(BaseModel):
    axis: str
    value: float
    confirmed: bool = False

    @field_validator("value")
    @classmethod
    def _v(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("value must be in [0, 1]")
        return v


class HedgePayload(BaseModel):
    value: float = Field(..., ge=0.0, le=1.0)


class NeverUsePayload(BaseModel):
    phrases: list[str]


class CircadianPayload(BaseModel):
    table: dict[int, float]


class SafetyCapsPayload(BaseModel):
    daily_cap_minutes: Optional[int] = None
    reality_anchor_rate_denominator: Optional[int] = None
    bad_day_probability_per_day: Optional[float] = None
    confirm: Optional[str] = None


class VoiceParamsPayload(BaseModel):
    stability: Optional[float] = None
    similarity_boost: Optional[float] = None
    style: Optional[float] = None


class TagPayload(BaseModel):
    tag: str
    day: Optional[str] = None
    turn_ts: Optional[float] = None
    note: str = ""

    @field_validator("tag")
    @classmethod
    def _t(cls, v: str) -> str:
        if v not in (TAG_IMMERSION_BREAK, TAG_HIT, TAG_PAUSE):
            raise ValueError(f"unknown tag: {v}")
        return v


class PausePayload(BaseModel):
    hours: int = 24
    reason: str = ""
    confirm: Optional[str] = None


class PresenceScorePayload(BaseModel):
    score: int = Field(..., ge=1, le=5)


class SessionNotesPayload(BaseModel):
    notes: str


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def build_app(
    cfg: DashboardConfig,
    *,
    orchestrator: Any = None,
    safety_layer: Any = None,
) -> FastAPI:
    cfg.validate()
    app = FastAPI(
        title="Renée M15 Dashboard",
        version="1.0",
        docs_url=None,
        redoc_url=None,
    )

    state_dir = Path(cfg.state_dir)
    config_dir = Path(cfg.config_dir)
    audit = DashboardAuditLog(state_dir / "dashboard_actions.db")
    journal = M15Journal(state_dir / "m15_journal.db")
    agent = DashboardAgent(state_dir)
    sessions_root = Path(cfg.sessions_root) if cfg.sessions_root else default_sessions_root()

    persona_yaml = config_dir / f"{cfg.persona}.yaml"
    safety_yaml = config_dir / "safety.yaml"
    voice_yaml = config_dir / "voice.yaml"

    # ------------------------------------------------------------------
    # auth middleware
    # ------------------------------------------------------------------

    @app.middleware("http")
    async def require_password_when_external(request: Request, call_next):
        # Loopback bind implicitly trusts the caller.
        if not cfg.requires_password:
            return await call_next(request)
        # Any static/HTML route is still gated; fail closed.
        supplied = request.headers.get(HEADER_AUTH) or request.query_params.get(
            "password", ""
        )
        expected = cfg.password or ""
        if not expected or not supplied or not hmac.compare_digest(
            str(expected), str(supplied)
        ):
            return JSONResponse(
                status_code=401,
                content={"error": "dashboard password required"},
            )
        return await call_next(request)

    # ------------------------------------------------------------------
    # root
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_SPA_HTML)

    @app.get("/api/ping")
    async def ping() -> dict:
        return {"ok": True, "persona": cfg.persona, "ts": datetime.now().isoformat()}

    # ------------------------------------------------------------------
    # live tab
    # ------------------------------------------------------------------

    @app.get("/api/live/snapshot")
    async def api_live_snapshot() -> dict:
        return live_snapshot(
            state_dir=state_dir,
            config_dir=config_dir,
            persona=cfg.persona,
            orchestrator=orchestrator,
            safety_layer=safety_layer,
        )

    # ------------------------------------------------------------------
    # tuning tab
    # ------------------------------------------------------------------

    @app.get("/api/tuning/state")
    async def api_tuning_state() -> dict:
        persona_data = tuning_mod.load_yaml(persona_yaml)
        safety_data = tuning_mod.load_yaml(safety_yaml)
        voice_data = tuning_mod.load_yaml(voice_yaml)
        return {
            "persona": {
                "baseline_mood": persona_data.get("baseline_mood") or {},
                "circadian": persona_data.get("circadian") or {},
                "speech_patterns": persona_data.get("speech_patterns") or {},
                "opinions": persona_data.get("opinions") or {},
            },
            "safety": {
                "reality_anchors_rate_denominator": (safety_data.get("reality_anchors") or {}).get("rate_denominator", 50),
                "health_monitor": safety_data.get("health_monitor") or {},
                "bad_day": safety_data.get("bad_day") or {},
            },
            "voice": voice_data,
            "mood_axis_max_delta": cfg.mood_axis_max_delta,
            "confirm_token": cfg.confirm_token,
        }

    @app.post("/api/tuning/mood_baseline")
    async def api_tuning_mood_baseline(payload: MoodBaselinePayload) -> dict:
        if payload.axis not in tuning_mod.MOOD_AXES:
            raise HTTPException(status_code=400, detail=f"unknown axis: {payload.axis}")
        current = (tuning_mod.load_yaml(persona_yaml).get("baseline_mood") or {}).get(
            payload.axis, 0.5
        )
        delta = abs(float(payload.value) - float(current))
        if delta > cfg.mood_axis_max_delta and not payload.confirmed:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"delta {delta:.2f} exceeds cap {cfg.mood_axis_max_delta}; "
                    "re-submit with confirmed=true"
                ),
            )
        result = tuning_mod.update_mood_baseline(
            persona_yaml=persona_yaml,
            axis=payload.axis,
            value=payload.value,
            orchestrator=orchestrator,
        )
        receipt = agent.sign_action(
            field=result.field,
            old_value=result.old_value,
            new_value=result.new_value,
            confirmed=bool(payload.confirmed),
        )
        audit.record(
            field=result.field,
            old_value=result.old_value,
            new_value=result.new_value,
            confirmed=bool(payload.confirmed),
            actor="pj",
            receipt_id=receipt.receipt_id,
        )
        return {
            "ok": True,
            "field": result.field,
            "old_value": result.old_value,
            "new_value": result.new_value,
            "reload_ok": result.reload_ok,
            "receipt_id": receipt.receipt_id,
        }

    @app.post("/api/tuning/hedge_frequency")
    async def api_tuning_hedge(payload: HedgePayload) -> dict:
        result = tuning_mod.update_hedge_frequency(
            persona_yaml=persona_yaml,
            value=payload.value,
            orchestrator=orchestrator,
        )
        _log_change(audit, agent, result)
        return _dict_from_result(result)

    @app.post("/api/tuning/never_use")
    async def api_tuning_never_use(payload: NeverUsePayload) -> dict:
        result = tuning_mod.update_never_uses(
            persona_yaml=persona_yaml,
            phrases=payload.phrases,
            orchestrator=orchestrator,
        )
        _log_change(audit, agent, result)
        return _dict_from_result(result)

    @app.post("/api/tuning/circadian")
    async def api_tuning_circadian(payload: CircadianPayload) -> dict:
        result = tuning_mod.update_circadian(
            persona_yaml=persona_yaml,
            table=payload.table,
            orchestrator=orchestrator,
        )
        _log_change(audit, agent, result)
        return _dict_from_result(result)

    @app.post("/api/tuning/safety_caps")
    async def api_tuning_safety_caps(payload: SafetyCapsPayload) -> dict:
        if (payload.confirm or "") != cfg.confirm_token:
            raise HTTPException(
                status_code=409,
                detail=f"safety caps require confirm='{cfg.confirm_token}'",
            )
        result = tuning_mod.update_safety_caps(
            safety_yaml=safety_yaml,
            daily_cap_minutes=payload.daily_cap_minutes,
            reality_anchor_rate_denominator=payload.reality_anchor_rate_denominator,
            bad_day_probability_per_day=payload.bad_day_probability_per_day,
            safety_layer=safety_layer,
        )
        _log_change(audit, agent, result, confirmed=True)
        return _dict_from_result(result)

    @app.post("/api/tuning/voice_params")
    async def api_tuning_voice(payload: VoiceParamsPayload) -> dict:
        result = tuning_mod.update_voice_params(
            voice_yaml=voice_yaml,
            stability=payload.stability,
            similarity_boost=payload.similarity_boost,
            style=payload.style,
        )
        _log_change(audit, agent, result)
        return _dict_from_result(result)

    # ------------------------------------------------------------------
    # logs tab
    # ------------------------------------------------------------------

    @app.get("/api/logs/conversation")
    async def api_logs_conversation(day: str = Query(None)) -> dict:
        day_key = day or date.today().strftime("%Y-%m-%d")
        return logs_for_day(state_dir=state_dir, day_key=day_key)

    @app.post("/api/logs/tag")
    async def api_logs_tag(payload: TagPayload) -> dict:
        day_key = payload.day or date.today().strftime("%Y-%m-%d")
        entry = journal.tag(
            tag=payload.tag,
            day_key=day_key,
            turn_ts=payload.turn_ts,
            note=payload.note,
        )
        audit.record(
            field=f"journal.{payload.tag}",
            old_value="",
            new_value={"day": day_key, "note": payload.note, "turn_ts": payload.turn_ts},
            confirmed=True,
            actor="pj",
        )
        return {
            "ok": True,
            "id": entry.id,
            "tag": entry.tag,
            "day": entry.day_key,
            "turn_ts": entry.turn_ts,
            "note": entry.note,
        }

    @app.get("/api/logs/journal")
    async def api_logs_journal(day: str = Query(None)) -> dict:
        day_key = day or date.today().strftime("%Y-%m-%d")
        entries = journal.entries_for_day(day_key)
        return {
            "day": day_key,
            "entries": [
                {
                    "id": e.id,
                    "ts": e.ts,
                    "tag": e.tag,
                    "turn_ts": e.turn_ts,
                    "note": e.note,
                }
                for e in entries
            ],
            "counts_last_30d": journal.counts_by_tag(days=30),
        }

    @app.get("/api/logs/export", response_class=PlainTextResponse)
    async def api_logs_export(
        day: str = Query(...),
        fmt: str = Query("txt"),
    ) -> PlainTextResponse:
        data = logs_for_day(state_dir=state_dir, day_key=day)
        if fmt == "json":
            return PlainTextResponse(
                json.dumps(data, indent=2),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="renee-{day}.json"'},
            )
        body = "\n".join(data.get("lines", []))
        return PlainTextResponse(
            body,
            headers={"Content-Disposition": f'attachment; filename="renee-{day}.log"'},
        )

    # ------------------------------------------------------------------
    # health tab
    # ------------------------------------------------------------------

    @app.get("/api/health/summary")
    async def api_health_summary() -> dict:
        return health_snapshot(state_dir=state_dir, config_dir=config_dir)

    @app.post("/api/health/pause")
    async def api_health_pause(payload: PausePayload) -> dict:
        if (payload.confirm or "") != cfg.confirm_token:
            raise HTTPException(
                status_code=409,
                detail=f"pause requires confirm='{cfg.confirm_token}'",
            )
        hours = max(1, min(72, int(payload.hours)))
        # Reuse the bridge cooldown machinery for the manual pause: we
        # write directly to the bridge_cooldowns table via the safety
        # layer's health monitor when one is in-process, else we skip
        # the runtime side and just log the tag.
        tag_entry = journal.tag(
            tag=TAG_PAUSE,
            day_key=date.today().strftime("%Y-%m-%d"),
            note=f"{hours}h: {payload.reason}",
        )
        cooldown_until = None
        if safety_layer is not None:
            import time as _time
            cooldown_until = _time.time() + hours * 3600.0
            with safety_layer.health._conn() as c:  # internal but intentional
                c.execute(
                    "INSERT INTO bridge_cooldowns "
                    "(triggered_at, cooldown_until, day_key, reason, "
                    "minutes_used, minutes_cap) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _time.time(),
                        cooldown_until,
                        date.today().strftime("%Y-%m-%d"),
                        "manual_pause",
                        safety_layer.health.daily_minutes(),
                        float(safety_layer.cfg.health_monitor.daily_cap_minutes or 0),
                    ),
                )
        audit.record(
            field="health.manual_pause",
            old_value="",
            new_value={"hours": hours, "reason": payload.reason},
            confirmed=True,
            actor="pj",
        )
        return {
            "ok": True,
            "pause_id": tag_entry.id,
            "hours": hours,
            "cooldown_until": cooldown_until,
        }

    # ------------------------------------------------------------------
    # eval tab
    # ------------------------------------------------------------------

    @app.get("/api/eval/dashboard_path")
    async def api_eval_dashboard_path() -> dict:
        """Return the path to the nightly eval HTML dashboard if present.
        The existing eval harness writes state/eval_dashboard.html; the
        Eval tab surfaces a link to it rather than re-implementing the
        view."""
        path = state_dir / "eval_dashboard.html"
        return {"path": str(path), "exists": path.exists()}

    @app.get("/api/eval/summary")
    async def api_eval_summary() -> dict:
        # Pull a thin summary from metrics.db; the existing eval harness
        # owns deeper analyses.
        from ..eval.metrics import MetricsStore
        m = MetricsStore(state_dir)
        return m.session_summary()

    # ------------------------------------------------------------------
    # sessions tab
    # ------------------------------------------------------------------

    @app.get("/api/sessions/list")
    async def api_sessions_list() -> dict:
        return {"sessions": sessions_mod.list_sessions(sessions_root)}

    @app.get("/api/sessions/trends")
    async def api_sessions_trends() -> dict:
        return sessions_mod.session_trends(sessions_root)

    @app.get("/api/sessions/disk_usage")
    async def api_sessions_disk_usage() -> dict:
        return sessions_mod.disk_usage(sessions_root)

    @app.get("/api/sessions/{session_id}/detail")
    async def api_sessions_detail(session_id: str) -> dict:
        try:
            return sessions_mod.session_detail(sessions_root, session_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/api/sessions/{session_id}/audio/{name}")
    async def api_sessions_audio(session_id: str, name: str):
        try:
            path = sessions_mod.resolve_session_audio(
                sessions_root, session_id, name,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return FileResponse(
            str(path),
            media_type="audio/wav",
            filename=name,
        )

    @app.post("/api/sessions/{session_id}/presence_score")
    async def api_sessions_presence(session_id: str, payload: PresenceScorePayload) -> dict:
        try:
            manifest = sessions_mod.set_presence_score(
                sessions_root, session_id, int(payload.score),
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except PresenceScoreLockedError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        audit.record(
            field=f"sessions.{session_id}.presence_score",
            old_value="",
            new_value=int(payload.score),
            confirmed=True,
            actor="pj",
        )
        return {
            "ok": True,
            "session_id": session_id,
            "presence_score": manifest.get("presence_score"),
        }

    @app.post("/api/sessions/{session_id}/notes")
    async def api_sessions_notes(session_id: str, payload: SessionNotesPayload) -> dict:
        session_dir = sessions_root / session_id
        if not (session_dir / "session_manifest.json").exists():
            raise HTTPException(status_code=404, detail="session not found")
        notes_path = session_dir / "notes.md"
        notes_path.write_text(payload.notes, encoding="utf-8")
        audit.record(
            field=f"sessions.{session_id}.notes",
            old_value="",
            new_value={"bytes": len(payload.notes)},
            confirmed=True,
            actor="pj",
        )
        return {"ok": True, "session_id": session_id, "bytes": len(payload.notes)}

    # ------------------------------------------------------------------
    # audit trail (meta)
    # ------------------------------------------------------------------

    @app.get("/api/audit/recent")
    async def api_audit_recent(limit: int = Query(50, ge=1, le=500)) -> dict:
        return {
            "agent_id": agent.agent_id,
            "count": audit.count(),
            "entries": [a.as_dict() for a in audit.recent(limit=limit)],
        }

    # Expose the internals the tests reach for.
    app.state.audit = audit
    app.state.journal = journal
    app.state.agent = agent
    app.state.cfg = cfg
    return app


# ---------------------------------------------------------------------------
# private helpers
# ---------------------------------------------------------------------------


def _dict_from_result(result: tuning_mod.TuningResult) -> dict:
    return {
        "ok": True,
        "field": result.field,
        "old_value": result.old_value,
        "new_value": result.new_value,
        "reload_ok": result.reload_ok,
    }


def _log_change(
    audit: DashboardAuditLog,
    agent: DashboardAgent,
    result: tuning_mod.TuningResult,
    *,
    confirmed: bool = False,
) -> None:
    receipt = agent.sign_action(
        field=result.field,
        old_value=result.old_value,
        new_value=result.new_value,
        confirmed=confirmed,
    )
    audit.record(
        field=result.field,
        old_value=result.old_value,
        new_value=result.new_value,
        confirmed=confirmed,
        actor="pj",
        receipt_id=receipt.receipt_id,
    )


# ---------------------------------------------------------------------------
# single-page HTML
# ---------------------------------------------------------------------------


_SPA_HTML = """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>Renée M15 Dashboard</title>
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin: 0; font: 14px/1.4 -apple-system, Segoe UI, Roboto, sans-serif; background: #0f1115; color: #e8e8e8; }
 header { padding: 14px 18px; border-bottom: 1px solid #22262e; display: flex; align-items: center; gap: 14px; }
 header h1 { font-size: 16px; margin: 0; font-weight: 600; }
 nav { display: flex; gap: 6px; }
 nav button { background: transparent; border: 1px solid #2a2f38; color: #cdd3dc; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
 nav button.active { background: #1f2733; border-color: #415168; color: #fff; }
 main { padding: 18px; }
 section { display: none; }
 section.active { display: block; }
 .bar { background: #1a1e26; border-radius: 6px; height: 14px; overflow: hidden; position: relative; }
 .bar .fill { height: 100%; background: linear-gradient(90deg, #2e88ff, #4cd1a1); }
 .bar .tick { position: absolute; top: -4px; width: 2px; height: 22px; background: #f3c34c; }
 .axis { display: grid; grid-template-columns: 110px 1fr 60px; gap: 10px; align-items: center; margin-bottom: 6px; }
 table { width: 100%; border-collapse: collapse; }
 th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #1b1f28; vertical-align: top; }
 th { font-weight: 500; color: #9aa3b2; }
 .card { background: #151922; border: 1px solid #1f2430; border-radius: 8px; padding: 14px; margin-bottom: 14px; }
 .row { display: flex; gap: 12px; flex-wrap: wrap; }
 .row > .card { flex: 1 1 280px; }
 button.action { background: #2e88ff; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
 button.danger { background: #d95f5f; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
 input, select, textarea { background: #0f1218; color: #e8e8e8; border: 1px solid #2a2f38; border-radius: 4px; padding: 4px 6px; }
 .status-good { color: #4cd1a1; }
 .status-warn { color: #f3c34c; }
 .status-bad { color: #ff6161; }
 small { color: #8b93a2; }
</style>
</head>
<body>
<header>
 <h1>Renée M15</h1>
 <nav>
  <button data-tab="live" class="active">Live</button>
  <button data-tab="tuning">Tuning</button>
  <button data-tab="logs">Logs</button>
  <button data-tab="health">Health</button>
  <button data-tab="eval">Eval</button>
  <button data-tab="sessions">Sessions</button>
 </nav>
 <span id="status" style="margin-left:auto;"><small>&nbsp;</small></span>
</header>
<main>
 <section id="tab-live" class="active">
  <div class="row">
   <div class="card">
    <h3>Mood</h3>
    <div id="mood-axes"></div>
    <small id="mood-bad-day"></small>
   </div>
   <div class="card">
    <h3>Bridge</h3>
    <div id="bridge"></div>
   </div>
   <div class="card">
    <h3>Latency</h3>
    <div id="latency"></div>
   </div>
   <div class="card">
    <h3>Anchors</h3>
    <div id="anchor"></div>
   </div>
  </div>
  <div class="card">
   <h3>Last turns</h3>
   <div id="last-turns"></div>
  </div>
 </section>

 <section id="tab-tuning">
  <div class="card">
   <h3>Mood baseline</h3>
   <div id="tuning-mood"></div>
  </div>
  <div class="card">
   <h3>Speech</h3>
   <label>hedge frequency <input type="number" step="0.01" id="hedge-input" min="0" max="1"></label>
   <button class="action" id="hedge-save">Save</button>
  </div>
  <div class="card">
   <h3>Never-use phrases</h3>
   <textarea id="never-use" rows="4" style="width:100%"></textarea>
   <button class="action" id="never-use-save">Save</button>
  </div>
  <div class="card">
   <h3>Safety caps <small>(requires typing confirm)</small></h3>
   <div class="row">
    <label>daily cap minutes <input type="number" id="cap-minutes" min="0"></label>
    <label>anchor rate 1 in <input type="number" id="anchor-rate" min="1"></label>
    <label>bad day p/day <input type="number" step="0.01" id="bad-day" min="0" max="1"></label>
    <label>confirm <input id="confirm-token" placeholder="confirm"></label>
   </div>
   <button class="action" id="caps-save">Save</button>
  </div>
 </section>

 <section id="tab-logs">
  <div class="card">
   <label>Day <input type="date" id="log-day"></label>
   <button class="action" id="log-load">Load</button>
   <button class="action" id="log-export-txt">Export txt</button>
   <button class="action" id="log-export-json">Export json</button>
  </div>
  <div class="card"><div id="log-lines" style="white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace"></div></div>
  <div class="card">
   <h3>Tag a moment</h3>
   <select id="tag-kind">
    <option value="immersion_break">Immersion break</option>
    <option value="hit">Hit (too real)</option>
   </select>
   <input id="tag-note" placeholder="note" style="width:60%">
   <button class="action" id="tag-save">Tag</button>
  </div>
  <div class="card"><h3>Journal</h3><div id="journal"></div></div>
 </section>

 <section id="tab-health">
  <div class="row">
   <div class="card" style="flex:1">
    <h3>Today</h3>
    <div id="health-today"></div>
   </div>
   <div class="card" style="flex:1">
    <h3>Averages</h3>
    <div id="health-avg"></div>
   </div>
   <div class="card" style="flex:1">
    <h3>Sycophancy</h3>
    <div id="health-syc"></div>
   </div>
  </div>
  <div class="card">
   <h3>30-day daily minutes</h3>
   <div id="health-30d"></div>
  </div>
  <div class="card">
   <h3>Manual pause</h3>
   <label>hours <input type="number" id="pause-hours" value="24" min="1" max="72"></label>
   <input id="pause-reason" placeholder="reason (optional)" style="width:40%">
   <input id="pause-confirm" placeholder="confirm">
   <button class="danger" id="pause-btn">Pause bridge</button>
  </div>
 </section>

 <section id="tab-eval">
  <div class="card">
   <h3>Eval summary</h3>
   <pre id="eval-summary" style="white-space:pre-wrap"></pre>
   <a id="eval-link" target="_blank">Open nightly HTML dashboard</a>
  </div>
 </section>

 <section id="tab-sessions">
  <div class="card">
   <h3>Disk usage</h3>
   <div id="sessions-disk"></div>
  </div>
  <div class="card">
   <h3>Cross-session trends</h3>
   <div id="sessions-trends"></div>
  </div>
  <div class="card">
   <h3>Sessions</h3>
   <table id="sessions-table">
    <thead><tr><th>session</th><th>date</th><th>duration</th><th>backend</th><th>flags</th><th>presence</th><th>publish</th></tr></thead>
    <tbody id="sessions-rows"><tr><td colspan="7"><small>loading...</small></td></tr></tbody>
   </table>
  </div>
  <div class="card" id="session-detail-card" style="display:none">
   <h3>Session detail <small id="session-detail-id"></small></h3>
   <div id="session-detail-body"></div>
  </div>
 </section>
</main>

<script>
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const tabs = ["live", "tuning", "logs", "health", "eval", "sessions"];
let tuningState = null;

$$("nav button").forEach(b => b.onclick = () => {
  const t = b.dataset.tab;
  $$("nav button").forEach(x => x.classList.toggle("active", x === b));
  tabs.forEach(n => $("#tab-" + n).classList.toggle("active", n === t));
  refreshTab(t);
});

async function api(path, opts = {}) {
  const res = await fetch(path, Object.assign({headers: {"content-type": "application/json"}}, opts));
  if (!res.ok) throw new Error(res.status + " " + await res.text());
  return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
}

async function refreshTab(name) {
  if (name === "live") { await loadLive(); }
  if (name === "tuning") { await loadTuning(); }
  if (name === "logs") { await loadLogs(); }
  if (name === "health") { await loadHealth(); }
  if (name === "eval") { await loadEval(); }
  if (name === "sessions") { await loadSessions(); }
}

async function loadLive() {
  const s = await api("/api/live/snapshot");
  const moodEl = $("#mood-axes");
  moodEl.innerHTML = "";
  for (const axis of s.mood.current) {
    const bl = s.mood.baseline.find(b => b.axis === axis.axis);
    const row = document.createElement("div");
    row.className = "axis";
    row.innerHTML = `<span>${axis.axis}</span><div class="bar"><div class="fill" style="width:${axis.value*100}%"></div><div class="tick" style="left:${(bl?.value||0)*100}%"></div></div><span>${axis.value.toFixed(2)}</span>`;
    moodEl.appendChild(row);
  }
  $("#mood-bad-day").textContent = s.mood.bad_day ? "bad day active" : "";
  const bridgeEl = $("#bridge");
  bridgeEl.innerHTML = `<div>status: <b class="${s.bridge.allowed ? 'status-good' : 'status-bad'}">${s.bridge.allowed ? 'allowed' : 'cooldown'}</b></div>`;
  if (s.bridge.cooldown_until) bridgeEl.innerHTML += `<div><small>cooldown until ${new Date(s.bridge.cooldown_until*1000).toLocaleString()}</small></div>`;
  $("#latency").innerHTML = `p50 ${s.latency.p50_ms}ms<br>p95 ${s.latency.p95_ms}ms<br>backend: ${s.latency.last_backend || '?'}<br>turns: ${s.latency.count}`;
  $("#anchor").innerHTML = `rate: ${(s.anchor.rate*100).toFixed(2)}%<br>last: ${s.anchor.last_phrase || '—'}<br>count: ${s.anchor.count}`;
  const turnsEl = $("#last-turns");
  turnsEl.innerHTML = "";
  const table = document.createElement("table");
  table.innerHTML = "<tr><th>ts</th><th>turn</th><th>backend</th><th>latency</th></tr>";
  for (const t of s.last_turns.slice().reverse()) {
    const tr = document.createElement("tr");
    const tl = t.telemetry || {};
    tr.innerHTML = `<td>${new Date((t.ts||0)*1000).toLocaleTimeString()}</td><td>${t.response_chars||''} chars</td><td>${tl.persona_backend||'?'}</td><td>${(tl.total_ms||0).toFixed?.(0)||''}ms</td>`;
    table.appendChild(tr);
  }
  turnsEl.appendChild(table);
  $("#status").innerHTML = `<small>synced ${new Date().toLocaleTimeString()}</small>`;
}

async function loadTuning() {
  tuningState = await api("/api/tuning/state");
  const moodEl = $("#tuning-mood");
  moodEl.innerHTML = "";
  const axes = ["energy","warmth","playfulness","focus","patience","curiosity"];
  for (const axis of axes) {
    const cur = tuningState.persona.baseline_mood[axis] ?? 0.5;
    const row = document.createElement("div");
    row.className = "axis";
    row.innerHTML = `<label>${axis}</label><input type="range" min="0" max="1" step="0.01" value="${cur}" data-axis="${axis}"><span class="val">${cur.toFixed(2)}</span>`;
    moodEl.appendChild(row);
    const input = row.querySelector("input");
    const val = row.querySelector(".val");
    input.oninput = () => { val.textContent = parseFloat(input.value).toFixed(2); };
    input.onchange = async () => {
      let confirmed = false;
      if (Math.abs(parseFloat(input.value) - cur) > tuningState.mood_axis_max_delta) {
        confirmed = confirm(`Delta exceeds ${tuningState.mood_axis_max_delta}. Confirm?`);
        if (!confirmed) { input.value = cur; val.textContent = cur.toFixed(2); return; }
      }
      await api("/api/tuning/mood_baseline", {method:"POST", body: JSON.stringify({axis, value: parseFloat(input.value), confirmed})});
      tuningState = await api("/api/tuning/state");
    };
  }
  $("#hedge-input").value = tuningState.persona.speech_patterns?.hedge_frequency ?? 0.3;
  $("#hedge-save").onclick = async () => {
    await api("/api/tuning/hedge_frequency", {method:"POST", body: JSON.stringify({value: parseFloat($("#hedge-input").value)})});
  };
  $("#never-use").value = (tuningState.persona.speech_patterns?.never_uses || []).join("\\n");
  $("#never-use-save").onclick = async () => {
    const phrases = $("#never-use").value.split("\\n").map(s=>s.trim()).filter(Boolean);
    await api("/api/tuning/never_use", {method:"POST", body: JSON.stringify({phrases})});
  };
  $("#cap-minutes").value = tuningState.safety.health_monitor?.daily_cap_minutes ?? 120;
  $("#anchor-rate").value = tuningState.safety.reality_anchors_rate_denominator ?? 50;
  $("#bad-day").value = tuningState.safety.bad_day?.probability_per_day ?? (1/15);
  $("#caps-save").onclick = async () => {
    const payload = {
      daily_cap_minutes: parseInt($("#cap-minutes").value, 10),
      reality_anchor_rate_denominator: parseInt($("#anchor-rate").value, 10),
      bad_day_probability_per_day: parseFloat($("#bad-day").value),
      confirm: $("#confirm-token").value,
    };
    await api("/api/tuning/safety_caps", {method:"POST", body: JSON.stringify(payload)});
  };
}

async function loadLogs() {
  const today = new Date().toISOString().slice(0,10);
  if (!$("#log-day").value) $("#log-day").value = today;
  $("#log-load").onclick = async () => {
    const d = $("#log-day").value;
    const j = await api(`/api/logs/conversation?day=${encodeURIComponent(d)}`);
    $("#log-lines").textContent = j.exists ? j.lines.join("\\n") : `no log for ${d}`;
    const jr = await api(`/api/logs/journal?day=${encodeURIComponent(d)}`);
    const tbl = document.createElement("table");
    tbl.innerHTML = "<tr><th>ts</th><th>tag</th><th>note</th></tr>";
    for (const e of jr.entries) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${new Date(e.ts*1000).toLocaleString()}</td><td>${e.tag}</td><td>${e.note}</td>`;
      tbl.appendChild(tr);
    }
    $("#journal").innerHTML = "";
    $("#journal").appendChild(tbl);
  };
  $("#log-export-txt").onclick = () => { const d = $("#log-day").value; window.location = `/api/logs/export?day=${d}&fmt=txt`; };
  $("#log-export-json").onclick = () => { const d = $("#log-day").value; window.location = `/api/logs/export?day=${d}&fmt=json`; };
  $("#tag-save").onclick = async () => {
    const tag = $("#tag-kind").value;
    const note = $("#tag-note").value;
    await api("/api/logs/tag", {method:"POST", body: JSON.stringify({tag, day: $("#log-day").value, note})});
    $("#tag-note").value = "";
    $("#log-load").onclick();
  };
  $("#log-load").onclick();
}

async function loadHealth() {
  const j = await api("/api/health/summary");
  $("#health-today").innerHTML = `today: ${j.daily_minutes}m of ${j.daily_cap_minutes}m<br><div class="bar"><div class="fill" style="width:${Math.min(100, (j.daily_minutes/(j.daily_cap_minutes||1))*100).toFixed(1)}%"></div></div>`;
  $("#health-avg").innerHTML = `7d: ${j.seven_day_avg_minutes}m<br>30d: ${j.thirty_day_avg_minutes}m`;
  $("#health-syc").innerHTML = `rate: ${(j.sycophancy_rate*100).toFixed(2)}%`;
  const rows30 = j.rolling_30_day.map(r => `<tr><td>${r.day}</td><td><div class="bar"><div class="fill" style="width:${Math.min(100,(r.minutes/(j.daily_cap_minutes||1))*100).toFixed(1)}%"></div></div></td><td>${r.minutes}m</td></tr>`).join("");
  $("#health-30d").innerHTML = `<table>${rows30}</table>`;
  $("#pause-btn").onclick = async () => {
    const hours = parseInt($("#pause-hours").value, 10) || 24;
    const reason = $("#pause-reason").value;
    const confirmed = $("#pause-confirm").value;
    await api("/api/health/pause", {method:"POST", body: JSON.stringify({hours, reason, confirm: confirmed})});
  };
}

async function loadEval() {
  const s = await api("/api/eval/summary");
  $("#eval-summary").textContent = JSON.stringify(s, null, 2);
  const e = await api("/api/eval/dashboard_path");
  $("#eval-link").style.display = e.exists ? 'inline' : 'none';
  $("#eval-link").textContent = e.exists ? `Open nightly HTML: ${e.path}` : '';
}

function fmtDuration(s) {
  if (!s || s <= 0) return "-";
  const m = Math.floor(s / 60);
  const sec = Math.round(s - m * 60);
  return `${m}m${sec.toString().padStart(2,'0')}s`;
}

function fmtBytes(b) {
  if (!b || b <= 0) return "0 B";
  const u = ["B","KB","MB","GB","TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i += 1; }
  return `${b.toFixed(1)} ${u[i]}`;
}

async function loadSessions() {
  const disk = await api("/api/sessions/disk_usage");
  let warn = disk.soft_warn_at_80pct ? '<span class="status-warn">(&gt;80% used)</span>' : '';
  $("#sessions-disk").innerHTML = `
    <div>sessions total: ${fmtBytes(disk.sessions_total_bytes)} (${disk.session_count} sessions)</div>
    <div>free space: ${fmtBytes(disk.free_bytes)} / ${fmtBytes(disk.total_drive_bytes)} ${warn}</div>
    <div>avg session: ${fmtBytes(disk.avg_session_bytes)}; estimated runway: ${disk.days_of_runway} sessions</div>
  `;

  const trends = await api("/api/sessions/trends");
  if (!trends.count) {
    $("#sessions-trends").innerHTML = '<small>no sessions captured yet</small>';
  } else {
    const last7 = trends.sessions.slice(0, 7);
    const rows = last7.map(s => `<tr>
      <td>${s.session_id}</td>
      <td>${s.flag_total}</td>
      <td>${(s.latency_p50_s||0).toFixed(2)}</td>
      <td>${(s.latency_p95_s||0).toFixed(2)}</td>
      <td>${s.safety_count}</td>
      <td>${s.overlap_count}</td>
      <td>${s.presence_score ?? '-'}</td>
    </tr>`).join("");
    $("#sessions-trends").innerHTML = `<table>
      <thead><tr><th>session</th><th>flags</th><th>p50</th><th>p95</th><th>safety</th><th>overlap</th><th>presence</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  const list = await api("/api/sessions/list");
  if (!list.sessions.length) {
    $("#sessions-rows").innerHTML = '<tr><td colspan="7"><small>no sessions yet</small></td></tr>';
  } else {
    $("#sessions-rows").innerHTML = list.sessions.map(s => {
      const pub = s.github_published ? 'published' : (s.public ? 'public' : 'private');
      return `<tr data-session-id="${s.session_id}" style="cursor:pointer">
        <td>${s.session_id}</td>
        <td>${(s.start_time||'').slice(0,10)}</td>
        <td>${fmtDuration(s.duration_s)}</td>
        <td>${s.backend_used || ''}</td>
        <td>${s.flag_count}</td>
        <td>${s.presence_score ?? '-'}</td>
        <td>${pub}</td>
      </tr>`;
    }).join("");
    $$("#sessions-rows tr").forEach(tr => {
      tr.onclick = () => loadSessionDetail(tr.dataset.sessionId);
    });
  }
}

async function loadSessionDetail(sessionId) {
  const d = await api(`/api/sessions/${encodeURIComponent(sessionId)}/detail`);
  $("#session-detail-card").style.display = 'block';
  $("#session-detail-id").textContent = sessionId;
  const locked = d.manifest.github_published;
  const flagRows = d.flags.map(f => {
    const ts = f.timestamp !== null && f.timestamp !== undefined
      ? `<a href="#" data-ts="${f.timestamp}" class="flag-ts">${f.timestamp.toFixed(1)}s</a>`
      : '-';
    return `<tr><td>${ts}</td><td>${f.category}</td><td>${f.severity}</td><td>${f.description}</td></tr>`;
  }).join("");
  $("#session-detail-body").innerHTML = `
    <div class="row">
      <div class="card" style="flex:1">
        <h4>Audio</h4>
        <audio id="sess-mic" controls src="${d.mic_wav_url}"></audio>
        <div><small>mic.wav</small></div>
        <audio id="sess-renee" controls src="${d.renee_wav_url}"></audio>
        <div><small>renee.wav</small></div>
      </div>
      <div class="card" style="flex:1">
        <h4>Latency</h4>
        <div>count: ${d.latency.count||0}</div>
        <div>p50: ${(d.latency.p50_s||0).toFixed(2)}s</div>
        <div>p95: ${(d.latency.p95_s||0).toFixed(2)}s</div>
        <div>p99: ${(d.latency.p99_s||0).toFixed(2)}s</div>
      </div>
      <div class="card" style="flex:1">
        <h4>Presence score</h4>
        <input type="number" min="1" max="5" id="presence-input"
               value="${d.manifest.presence_score ?? ''}" ${locked ? 'disabled' : ''}>
        <button class="action" id="presence-save" ${locked ? 'disabled' : ''}>Save</button>
        ${locked ? '<div><small class="status-warn">locked after publish</small></div>' : ''}
      </div>
    </div>
    <div class="card">
      <h4>Flags (${d.flags.length})</h4>
      <table><thead><tr><th>ts</th><th>category</th><th>severity</th><th>description</th></tr></thead>
      <tbody>${flagRows || '<tr><td colspan="4"><small>no flags</small></td></tr>'}</tbody></table>
    </div>
    <div class="card">
      <h4>Notes</h4>
      <textarea id="session-notes" rows="8" style="width:100%">${d.notes}</textarea>
      <button class="action" id="notes-save">Save notes</button>
    </div>
  `;
  const preroll = 5.0;
  $$("#session-detail-body .flag-ts").forEach(a => {
    a.onclick = (e) => {
      e.preventDefault();
      const ts = Math.max(0, parseFloat(a.dataset.ts) - preroll);
      const player = $("#sess-renee");
      if (player) { player.currentTime = ts; player.play(); }
    };
  });
  if (!locked) {
    $("#presence-save").onclick = async () => {
      const val = parseInt($("#presence-input").value, 10);
      try {
        await api(`/api/sessions/${encodeURIComponent(sessionId)}/presence_score`, {
          method: "POST", body: JSON.stringify({score: val}),
        });
        await loadSessions();
        await loadSessionDetail(sessionId);
      } catch (err) {
        alert(err.message || String(err));
      }
    };
  }
  $("#notes-save").onclick = async () => {
    const body = JSON.stringify({notes: $("#session-notes").value});
    await api(`/api/sessions/${encodeURIComponent(sessionId)}/notes`, {
      method: "POST", body,
    });
  };
}

refreshTab("live");
setInterval(() => { const active = document.querySelector("nav button.active").dataset.tab; refreshTab(active); }, 2000);
</script>
</body>
</html>
"""
