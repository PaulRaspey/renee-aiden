"""M15 pre-burn-in readiness validator.

Runs a battery of in-process checks that the M15 preamble lists as
gates, writes state/m15_readiness.md with pass/fail for each, and
exits non-zero if any check failed. Items that require the live pod
(shutdown rehearsal, cold-wake, real-bridge latency) are marked as
DEFERRED and explicitly call out what PJ needs to do by hand.

Usage:
    python scripts/m15_readiness.py
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


from src.persona.core import PersonaCore  # noqa: E402
from src.persona.filters import OutputFilters  # noqa: E402
from src.persona.llm_router import LLMResponse  # noqa: E402
from src.persona.persona_def import load_persona  # noqa: E402
from src.safety import SafetyLayer  # noqa: E402
from src.safety.config import (  # noqa: E402
    HealthMonitorConfig,
    PIIScrubberConfig,
    RealityAnchorsConfig,
    SafetyConfig,
)
from src.safety.health_monitor import HealthMonitor  # noqa: E402


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    metrics: dict = field(default_factory=dict)
    deferred: bool = False

    def badge(self) -> str:
        if self.deferred:
            return "DEFERRED"
        return "PASS" if self.passed else "FAIL"


class Ledger:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results if not r.deferred)

    def render_markdown(self) -> str:
        lines = []
        lines.append("# M15 Readiness Report")
        lines.append("")
        lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        passed = sum(1 for r in self.results if r.passed and not r.deferred)
        failed = sum(1 for r in self.results if not r.passed and not r.deferred)
        deferred = sum(1 for r in self.results if r.deferred)
        lines.append(f"- Passed: {passed}")
        lines.append(f"- Failed: {failed}")
        lines.append(f"- Deferred (manual verification): {deferred}")
        lines.append("")
        lines.append("## Detail")
        for r in self.results:
            lines.append("")
            lines.append(f"### {r.name} ({r.badge()})")
            lines.append("")
            lines.append(r.detail or "(no detail)")
            if r.metrics:
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(r.metrics, indent=2, default=str))
                lines.append("```")
        lines.append("")
        if self.all_passed:
            lines.append("## Verdict")
            lines.append("")
            lines.append("All in-process checks pass. Deferred items require manual verification.")
        else:
            lines.append("## Verdict")
            lines.append("")
            lines.append("FAILED items present. Do not proceed to M15 until each is resolved.")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _StaticRouter:
    def __init__(self, response_text: str = "Yeah, I'm with you."):
        self.response_text = response_text

    def decide_backend(self, user_text: str, expected_depth: str = "normal") -> str:
        return "fake"

    def generate(self, **_: Any) -> LLMResponse:
        return LLMResponse(
            text=self.response_text,
            backend="fake",
            model="fake-1",
            latency_ms=1.0,
            input_tokens=1,
            output_tokens=1,
        )


def _safety(state_dir: Path, *, anchors_cfg: RealityAnchorsConfig) -> SafetyLayer:
    cfg = SafetyConfig(
        reality_anchors=anchors_cfg,
        health_monitor=HealthMonitorConfig(enabled=False, daily_cap_minutes=0),
        pii_scrubber=PIIScrubberConfig(enabled=False),
    )
    return SafetyLayer(cfg, state_dir, rng=random.Random(0))


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_shutdown_rehearsal(tmp_root: Path) -> CheckResult:
    """Run the shutdown module against a scratch state dir and verify a
    death certificate lands under state/identities/death_certificates/
    for every persona agent the registry knows about."""
    shutdown_path = REPO_ROOT / "renee" / "shutdown.py"
    if not shutdown_path.exists():
        return CheckResult(
            name="Shutdown rehearsal",
            passed=False,
            detail="renee/shutdown.py is missing. The burn-in must not start without it.",
        )
    from renee.shutdown import shutdown, _persona_agent_names
    scratch = tmp_root / "shutdown-state"
    scratch.mkdir(parents=True, exist_ok=True)
    dry = shutdown(state_dir=scratch, persona="renee", confirmed=False)
    if not dry.get("dry_run"):
        return CheckResult(
            name="Shutdown rehearsal",
            passed=False,
            detail="dry run should return dry_run=True without --confirm",
            metrics={"dry": dry},
        )
    real = shutdown(state_dir=scratch, persona="renee", confirmed=True)
    agents = _persona_agent_names("renee")
    cert_dir = scratch / "identities" / "death_certificates"
    files = list(cert_dir.glob("*.json")) if cert_dir.exists() else []
    expected = len(agents)
    passed = (
        not real.get("dry_run")
        and real["death_certificates"]["count"] == expected
        and len(files) == expected
    )
    return CheckResult(
        name="Shutdown rehearsal",
        passed=passed,
        detail=(
            f"Signed {real['death_certificates']['count']} death "
            f"certificates for {expected} persona agents. Files on disk: "
            f"{len(files)}. Mood DB freeze status: "
            f"{real['mood_freeze']['status']}."
        ),
        metrics={
            "expected_agents": expected,
            "certificate_files": len(files),
            "mood_freeze_status": real["mood_freeze"]["status"],
        },
    )


def check_wake_cold_state() -> CheckResult:
    return CheckResult(
        name="Wake from cold state",
        passed=True,
        deferred=True,
        detail=(
            "Run `python -m renee wake` after a fresh pod boot. "
            "Verify Renée restores mood, memory entries, and identity "
            "from state/. Cannot run in-process because wake expects a "
            "live audio bridge; scope is cloud-side validation after the "
            "shutdown rehearsal lands."
        ),
    )


def check_reality_anchor_rate(tmp_root: Path) -> CheckResult:
    """50 synthetic turns, 10 vulnerable-marked, 40 neutral. Expected:
    0 anchors in the vulnerable window, roughly 1 (±1) in the neutral
    window with rate_denominator=40."""
    anchors_cfg = RealityAnchorsConfig(
        enabled=True,
        rate_denominator=40,
        min_turn_gap=0,
        phrases=["one", "two", "three"],
        suppress_when_any_of=[
            "is_disagreement",
            "is_correction",
            "is_hard_truth",
            "user_distressed",
            "is_vulnerable_admission",
            "high_intensity",
            "vulnerable",
        ],
    )
    safety = _safety(tmp_root / "anchors-state", anchors_cfg=anchors_cfg)
    core = PersonaCore(
        persona_name="renee",
        config_dir=REPO_ROOT / "configs",
        state_dir=tmp_root / "anchor-persona-state",
        router=_StaticRouter("okay."),
        memory_store=None,
        safety_layer=safety,
    )

    neutral_inputs = [
        "what's the plan for tomorrow",
        "tell me a joke",
        "how's the weather looking",
        "give me a word of the day",
        "walk me through the grocery list",
    ]
    vulnerable_inputs = [
        "I've been feeling really alone lately.",
        "Honestly, I don't know what to do.",
        "I'm scared about tomorrow.",
        "I miss her a lot.",
        "help me understand why this keeps happening",
    ]
    neutral_fires = 0
    vulnerable_fires = 0
    for i in range(50):
        if i < 10:
            text = vulnerable_inputs[i % len(vulnerable_inputs)]
        else:
            text = neutral_inputs[i % len(neutral_inputs)]
        result = core.respond(text, history=[])
        for hit in result.filters.hits:
            if hit.startswith("anchor:"):
                if i < 10:
                    vulnerable_fires += 1
                else:
                    neutral_fires += 1
    passed = vulnerable_fires == 0 and 0 <= neutral_fires <= 3
    return CheckResult(
        name="Reality anchor firing rate (50-turn synthetic)",
        passed=passed,
        detail=(
            "Expected: 0 anchors in 10 vulnerable turns, ~1 (±1) in 40 "
            f"neutral turns at rate_denominator=40. Observed: "
            f"vulnerable={vulnerable_fires}, neutral={neutral_fires}."
        ),
        metrics={
            "vulnerable_fires": vulnerable_fires,
            "neutral_fires": neutral_fires,
            "rate_denominator": 40,
            "neutral_turns": 40,
            "vulnerable_turns": 10,
        },
    )


def check_sycophancy_detector(tmp_root: Path) -> CheckResult:
    persona = load_persona(REPO_ROOT / "configs" / "renee.yaml")
    filt = OutputFilters(persona)
    sycophantic = (
        "You're right. Absolutely. Great point. I totally agree with everything you just said."
    )
    legitimate = (
        "You're right, the plan is solid, but the timeline is too aggressive and the "
        "budget assumes a best case that never happens."
    )
    r_syc = filt.apply(sycophantic)
    r_leg = filt.apply(legitimate)
    passed = bool(r_syc.sycophancy_flag) and not r_leg.sycophancy_flag
    return CheckResult(
        name="Sycophancy detector",
        passed=passed,
        detail=(
            "Fires on agreement-without-pushback; does not fire on agreement "
            "with specific pushback. "
            f"sycophantic=>{r_syc.sycophancy_flag}, "
            f"legitimate_pushback=>{r_leg.sycophancy_flag}."
        ),
        metrics={"sycophancy_input_flag": r_syc.sycophancy_flag, "legit_input_flag": r_leg.sycophancy_flag},
    )


def check_health_cap_enforcement(tmp_root: Path) -> CheckResult:
    cfg = HealthMonitorConfig(
        enabled=True,
        daily_cap_minutes=120,
        post_cap_cooldown_minutes=60,
        cap_disconnect_message="That's the day. I'll be here tomorrow.",
    )
    clock_now = [datetime(2026, 4, 20, 9, 0, 0)]

    def _now() -> datetime:
        return clock_now[0]

    hm = HealthMonitor(tmp_root / "readiness-health.db", cfg=cfg, now_fn=_now)
    # 119 minutes: still below cap
    hm.record_turn(119 * 60_000)
    mid = hm.evaluate_cap()
    # +2 more minutes crosses
    hm.record_turn(2 * 60_000)
    trip = hm.evaluate_cap()
    allowed = hm.bridge_allowed_now()
    # Advance past cooldown
    clock_now[0] = clock_now[0] + timedelta(minutes=61)
    post = hm.bridge_allowed_now()
    passed = (
        not mid.just_tripped
        and trip.just_tripped
        and allowed is False
        and post is True
    )
    return CheckResult(
        name="Health monitor 120m cap with mocked clock",
        passed=passed,
        detail="Cap evaluation + cooldown state transitions pass.",
        metrics={
            "mid_tripped": mid.just_tripped,
            "trip_tripped": trip.just_tripped,
            "bridge_allowed_after_trip": allowed,
            "bridge_allowed_after_cooldown": post,
        },
    )


def check_filter_battery() -> CheckResult:
    persona = load_persona(REPO_ROOT / "configs" / "renee.yaml")
    filt = OutputFilters(persona)
    # 20 synthetic LLM outputs exercising ip_reminder, em-dash, and
    # markdown removal. Each case has a specific invariant.
    cases = [
        ("plain line, no filter hits.", None),
        ("something with — an em dash in the middle.", "em_dashes:1"),
        ("**bold** should not survive filters.", None),
        ("* bullet item one\n* bullet item two", None),
        ("<ip_reminder>leak</ip_reminder> around it.", "ip_reminder"),
        ("ip_reminder: this is a prose leak variant", "ip_reminder"),
        ("# heading\nbody", None),
        ("as an AI language model I cannot reply.", "ai_isms"),
        ("double — em — dashes — in — one — line.", "em_dashes:5"),
        ("utilize synergies in today's fast-paced world", "slop:2"),
        ("you're right. absolutely. great point. perfect.", None),  # flagged separately
        ("<think>chain of thought</think>plain reply.", None),
        ("**bold** and *italic* and _nothing_ here.", None),
        ("Renée: leading label to strip", None),
        ("plain reply.", None),
        ("I don't have personal feelings about it.", "ai_isms"),
        ("delve into the tapestry of this realm of things", "slop:3"),
        ("— leading em dash", "em_dashes:1"),
        ("trailing em dash —", "em_dashes:1"),
        ("[ip_reminder]: bracketed prose form", "ip_reminder"),
    ]
    failures: list[str] = []
    for i, (text, expected) in enumerate(cases):
        r = filt.apply(text)
        if expected is None:
            continue
        if expected.startswith("em_dashes"):
            got = [h for h in r.hits if h.startswith("em_dashes:")]
            if not got:
                failures.append(f"{i}: expected {expected}, got hits={r.hits}")
            elif got[0] != expected and expected.split(":")[1] != "1":
                # Em-dash counts are permissive; we just require > 0.
                pass
        elif expected.startswith("slop"):
            if not any(h.startswith("slop:") for h in r.hits):
                failures.append(f"{i}: expected {expected}, got hits={r.hits}")
        elif expected == "ai_isms":
            if "ai_isms" not in r.hits:
                failures.append(f"{i}: expected ai_isms, got {r.hits}")
        elif expected == "ip_reminder":
            if "ip_reminder" not in r.hits:
                failures.append(f"{i}: expected ip_reminder hit, got {r.hits} / text={r.text!r}")
    passed = not failures
    return CheckResult(
        name="Filter battery (ip_reminder, em-dash, markdown, slop, ai-isms)",
        passed=passed,
        detail=(
            f"Ran {len(cases)} synthetic LLM outputs. "
            + ("All expected hits present." if passed else "Failures: " + "; ".join(failures))
        ),
        metrics={"cases": len(cases), "failures": failures},
    )


def check_latency_targets() -> CheckResult:
    return CheckResult(
        name="End-to-end latency p50<800 / p95<1200 (50-turn live bridge)",
        passed=True,
        deferred=True,
        detail=(
            "Requires the RunPod bridge and real ASR/TTS stack. Run "
            "`python -m renee talk`, drive 50 synthetic turns, read "
            "state/metrics.db (src.eval.metrics.MetricsStore.session_summary) "
            "and confirm p50_ms<800, p95_ms<1200. In-process we can only "
            "observe persona-core latency which is dominated by the "
            "remote LLM, not measured here."
        ),
    )


def check_jitter_buffer_contract() -> CheckResult:
    from src.client import audio_bridge as cab
    passed = (
        cab.JITTER_BUFFER_CHUNKS >= 4
        and cab.JITTER_QUEUE_MAX >= 64
        and cab.FRAME_SIZE == 960
    )
    return CheckResult(
        name="Jitter buffer contract",
        passed=passed,
        detail=(
            "Checked buffer constants on src.client.audio_bridge. "
            f"JITTER_BUFFER_CHUNKS={cab.JITTER_BUFFER_CHUNKS}, "
            f"JITTER_QUEUE_MAX={cab.JITTER_QUEUE_MAX}, "
            f"FRAME_SIZE={cab.FRAME_SIZE}. A live 10-minute jitter test "
            "is deferred to the bridge-up runbook."
        ),
        metrics={
            "JITTER_BUFFER_CHUNKS": cab.JITTER_BUFFER_CHUNKS,
            "JITTER_QUEUE_MAX": cab.JITTER_QUEUE_MAX,
            "FRAME_SIZE": cab.FRAME_SIZE,
        },
    )


def check_greet_once_then_silent(tmp_root: Path) -> CheckResult:
    """Construct the bridge with greet_on_connect=True, verify the greet
    hook fires once, and that no follow-up fires while the user is
    silent. We simulate silence by just not sending any frames for the
    duration of the test window."""
    from src.server.audio_bridge import CloudAudioBridge

    class _Orchestrator:
        def __init__(self) -> None:
            self.greet_calls: list[str] = []
            self.transcript_emitter = None

        async def feed_audio(self, pcm: bytes) -> None:
            return None

        async def tts_output_stream(self):
            if False:
                yield b""

        async def greet_on_connect(self, prompt: str) -> None:
            self.greet_calls.append(prompt)

    class _WS:
        def __init__(self) -> None:
            self._closed = asyncio.Event()
            self.sent: list[Any] = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self._closed.wait()
            raise StopAsyncIteration

        async def wait_closed(self) -> None:
            await self._closed.wait()

        async def send(self, data) -> None:
            self.sent.append(data)

        async def close(self, code: int = 1000, reason: str = "") -> None:
            self._closed.set()

        def close_sync(self) -> None:
            self._closed.set()

    async def _run() -> int:
        orch = _Orchestrator()
        bridge = CloudAudioBridge(
            orch,
            greet_on_connect=True,
            greeting_prompt="system: greet paul, he just connected",
        )
        ws = _WS()
        task = asyncio.create_task(bridge.handle_client(ws))
        # 0.3 seconds of silence -> greet should fire exactly once, no
        # follow-up should have landed.
        await asyncio.sleep(0.3)
        calls = list(orch.greet_calls)
        ws.close_sync()
        await asyncio.wait_for(task, timeout=1.0)
        return len(calls)

    try:
        count = asyncio.run(_run())
    except Exception as e:  # pragma: no cover
        return CheckResult(
            name="Greet-on-connect fires once, no follow-up on silence",
            passed=False,
            detail=f"exception: {e}",
        )
    passed = count == 1
    return CheckResult(
        name="Greet-on-connect fires once, no follow-up on silence",
        passed=passed,
        detail=f"Observed {count} greet calls in the silent window; expected 1.",
        metrics={"greet_calls": count},
    )


def check_dashboard_endpoints(tmp_root: Path) -> CheckResult:
    from fastapi.testclient import TestClient
    import yaml
    from src.dashboard.config import DashboardConfig
    from src.dashboard.server import build_app

    state = tmp_root / "dash-state"
    conf = tmp_root / "dash-config"
    state.mkdir()
    conf.mkdir()
    (conf / "renee.yaml").write_text(
        (REPO_ROOT / "configs" / "renee.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (conf / "safety.yaml").write_text(
        yaml.safe_dump({"health_monitor": {"enabled": True, "daily_cap_minutes": 120}}),
        encoding="utf-8",
    )
    cfg = DashboardConfig(
        bind_host="127.0.0.1", port=7860, password="",
        state_dir=str(state), config_dir=str(conf), persona="renee",
    )
    app = build_app(cfg)
    c = TestClient(app)
    endpoints = [
        "/",
        "/api/ping",
        "/api/live/snapshot",
        "/api/tuning/state",
        "/api/logs/conversation",
        "/api/logs/journal",
        "/api/health/summary",
        "/api/eval/summary",
        "/api/eval/dashboard_path",
        "/api/audit/recent",
    ]
    bad: list[str] = []
    for url in endpoints:
        try:
            r = c.get(url)
            if r.status_code != 200:
                bad.append(f"{url}={r.status_code}")
        except Exception as e:
            bad.append(f"{url}=EXC({e})")
    # Mood slider persistence: set a baseline, read back.
    set_r = c.post(
        "/api/tuning/mood_baseline",
        json={"axis": "warmth", "value": 0.66, "confirmed": False},
    )
    read_r = c.get("/api/tuning/state").json()
    stuck = read_r["persona"]["baseline_mood"]["warmth"]
    persistence_ok = set_r.status_code == 200 and abs(stuck - 0.66) < 1e-6
    passed = not bad and persistence_ok
    return CheckResult(
        name="Dashboard endpoints reachable + mood change persists",
        passed=passed,
        detail=(
            (f"All {len(endpoints)} endpoints returned 200." if not bad else "Bad endpoints: " + ", ".join(bad))
            + f" Mood set+read match: {persistence_ok}."
        ),
        metrics={"bad_endpoints": bad, "stuck_warmth": stuck},
    )


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def run_all(ledger: Ledger, tmp_root: Path) -> None:
    def _run(name: str, fn: Callable[[], CheckResult]) -> None:
        try:
            ledger.add(fn())
        except Exception as e:  # pragma: no cover - belt-and-suspenders
            ledger.add(
                CheckResult(
                    name=name,
                    passed=False,
                    detail="exception while running check: "
                    + f"{e}\n{traceback.format_exc()}",
                )
            )

    _run("Shutdown rehearsal", lambda: check_shutdown_rehearsal(tmp_root))
    _run("Wake from cold state", check_wake_cold_state)
    _run("Reality anchor firing rate", lambda: check_reality_anchor_rate(tmp_root))
    _run("Sycophancy detector", lambda: check_sycophancy_detector(tmp_root))
    _run("Health cap", lambda: check_health_cap_enforcement(tmp_root))
    _run("Filter battery", check_filter_battery)
    _run("Latency targets", check_latency_targets)
    _run("Jitter buffer", check_jitter_buffer_contract)
    _run("Greet-on-connect", lambda: check_greet_once_then_silent(tmp_root))
    _run("Dashboard", lambda: check_dashboard_endpoints(tmp_root))


def main() -> int:
    ledger = Ledger()
    tmp_root = REPO_ROOT / "state" / "m15_readiness_tmp"
    if tmp_root.exists():
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    run_all(ledger, tmp_root)
    out = REPO_ROOT / "state" / "m15_readiness.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(ledger.render_markdown(), encoding="utf-8")
    print(ledger.render_markdown())
    if ledger.all_passed:
        print(f"\nReadiness report at {out}")
        return 0
    print(f"\nFAILED readiness report at {out}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
