"""Tests for the M15 hard daily cap and bridge cooldown.

The cap is enforced by HealthMonitor.evaluate_cap(). The SafetyLayer facade
and the CloudAudioBridge use bridge_allowed_now() and cap_farewell() to gate
new connections while a cooldown is active. This file exercises all three
layers plus the persona core's turn-time override behaviour.
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.persona.core import PersonaCore
from src.persona.llm_router import LLMResponse
from src.safety import CapOutcome, SafetyLayer
from src.safety.config import (
    HealthMonitorConfig,
    PIIScrubberConfig,
    RealityAnchorsConfig,
    SafetyConfig,
)
from src.safety.health_monitor import CAP_REASON_DAILY, HealthMonitor
from src.server.audio_bridge import CloudAudioBridge


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


def _hm(
    tmp_path: Path,
    *,
    clock: FakeClock,
    cap_minutes: int = 120,
    cooldown_minutes: int = 60,
) -> HealthMonitor:
    cfg = HealthMonitorConfig(
        enabled=True,
        daily_minutes_soft_threshold=9999,
        daily_minutes_stronger_threshold=99999,
        sustained_days_soft=14,
        sustained_days_stronger=30,
        repeat_cooldown_days=14,
        daily_cap_minutes=cap_minutes,
        post_cap_cooldown_minutes=cooldown_minutes,
        cap_disconnect_message="That's the day. I'll be here tomorrow.",
    )
    return HealthMonitor(tmp_path / "health.db", cfg=cfg, now_fn=clock)


# ---------------------------------------------------------------------------
# health-monitor level
# ---------------------------------------------------------------------------


def test_evaluate_cap_returns_untripped_below_threshold(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 20, 8, 0, 0))
    hm = _hm(tmp_path, clock=clock)
    hm.record_turn(30 * 60_000)  # 30 minutes
    outcome = hm.evaluate_cap()
    assert outcome.just_tripped is False
    assert outcome.already_tripped is False
    assert outcome.minutes_used == pytest.approx(30.0, abs=0.1)
    assert outcome.minutes_cap == 120.0


def test_evaluate_cap_fires_once_on_threshold_crossing(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 20, 8, 0, 0))
    hm = _hm(tmp_path, clock=clock, cooldown_minutes=60)
    hm.record_turn(60 * 60_000)  # 60 min
    assert hm.evaluate_cap().just_tripped is False
    hm.record_turn(65 * 60_000)  # +65 min -> 125 min total
    outcome = hm.evaluate_cap()
    assert outcome.just_tripped is True
    assert outcome.cooldown_until is not None
    assert outcome.farewell == "That's the day. I'll be here tomorrow."
    # Second evaluation should NOT double-trip within the cooldown window.
    clock.advance(minutes=5)
    hm.record_turn(10 * 60_000)
    outcome2 = hm.evaluate_cap()
    assert outcome2.just_tripped is False
    assert outcome2.already_tripped is True


def test_bridge_allowed_now_honors_cooldown(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 20, 8, 0, 0))
    hm = _hm(tmp_path, clock=clock, cooldown_minutes=60)
    hm.record_turn(125 * 60_000)
    hm.evaluate_cap()
    assert hm.bridge_allowed_now() is False
    # Fast-forward past cooldown.
    clock.advance(minutes=61)
    assert hm.bridge_allowed_now() is True


def test_cap_zero_means_disabled(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 20, 8, 0, 0))
    hm = _hm(tmp_path, clock=clock, cap_minutes=0)
    hm.record_turn(600 * 60_000)
    outcome = hm.evaluate_cap()
    assert outcome.just_tripped is False
    assert hm.bridge_allowed_now() is True


def test_seven_and_thirty_day_averages(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 20, 8, 0, 0))
    hm = _hm(tmp_path, clock=clock)
    for d in range(7):
        hm.record_turn(30 * 60_000)
        clock.advance(days=1)
    # seven_day_average counts a trailing 7-day window including today.
    assert hm.seven_day_average_minutes() == pytest.approx(30.0, abs=5.0)
    assert hm.thirty_day_average_minutes() < hm.seven_day_average_minutes()


def test_latest_bridge_cooldown_returns_fields(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 20, 8, 0, 0))
    hm = _hm(tmp_path, clock=clock)
    hm.record_turn(125 * 60_000)
    hm.evaluate_cap()
    latest = hm.latest_bridge_cooldown()
    assert latest is not None
    assert latest["reason"] == CAP_REASON_DAILY
    assert latest["minutes_cap"] == 120.0
    assert latest["minutes_used"] >= 120.0


def test_new_day_resets_cap(tmp_path: Path):
    clock = FakeClock(datetime(2026, 4, 20, 8, 0, 0))
    hm = _hm(tmp_path, clock=clock, cooldown_minutes=60)
    hm.record_turn(125 * 60_000)
    hm.evaluate_cap()
    # Roll clock well past the cooldown and into the next day.
    clock.advance(days=1)
    assert hm.bridge_allowed_now() is True
    hm.record_turn(10 * 60_000)
    outcome = hm.evaluate_cap()
    assert outcome.just_tripped is False


# ---------------------------------------------------------------------------
# safety-facade level
# ---------------------------------------------------------------------------


def _safety_layer(tmp_path: Path, cap_minutes: int = 120, cooldown_minutes: int = 60) -> SafetyLayer:
    cfg = SafetyConfig(
        reality_anchors=RealityAnchorsConfig(enabled=False),
        health_monitor=HealthMonitorConfig(
            enabled=True,
            daily_minutes_soft_threshold=9999,
            daily_minutes_stronger_threshold=99999,
            sustained_days_soft=14,
            sustained_days_stronger=30,
            repeat_cooldown_days=14,
            daily_cap_minutes=cap_minutes,
            post_cap_cooldown_minutes=cooldown_minutes,
            cap_disconnect_message="That's the day. I'll be here tomorrow.",
        ),
        pii_scrubber=PIIScrubberConfig(enabled=False),
    )
    return SafetyLayer(cfg, tmp_path, rng=random.Random(0))


def test_safety_layer_record_turn_duration_returns_outcome(tmp_path: Path):
    safety = _safety_layer(tmp_path)
    outcome_a = safety.record_turn_duration(60 * 60_000)
    assert isinstance(outcome_a, CapOutcome)
    assert outcome_a.just_tripped is False
    outcome_b = safety.record_turn_duration(65 * 60_000)
    assert outcome_b.just_tripped is True
    assert safety.bridge_allowed_now() is False


def test_safety_layer_cap_farewell_comes_from_config(tmp_path: Path):
    safety = _safety_layer(tmp_path)
    assert safety.cap_farewell() == "That's the day. I'll be here tomorrow."


# ---------------------------------------------------------------------------
# persona-core integration
# ---------------------------------------------------------------------------


class FakeRouter:
    def __init__(self, response_text: str = "Yeah."):
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


def test_persona_core_overrides_text_on_cap_trip(tmp_path: Path):
    """When the cap trips, the persona core replaces the LLM reply with the
    farewell so TTS speaks the intended hand-off line, not whatever the LLM
    was about to say.

    Setup: a 1-minute cap and 59 minutes already accumulated on the health
    monitor for today. Any real turn duration pushes total above 60 seconds,
    tripping the cap.
    """
    cfg = SafetyConfig(
        reality_anchors=RealityAnchorsConfig(enabled=False),
        health_monitor=HealthMonitorConfig(
            enabled=True,
            daily_minutes_soft_threshold=9999,
            daily_minutes_stronger_threshold=99999,
            sustained_days_soft=14,
            sustained_days_stronger=30,
            repeat_cooldown_days=14,
            daily_cap_minutes=1,
            post_cap_cooldown_minutes=60,
            cap_disconnect_message="That's the day. I'll be here tomorrow.",
        ),
        pii_scrubber=PIIScrubberConfig(enabled=False),
    )
    safety = SafetyLayer(cfg, tmp_path / "safety-state", rng=random.Random(0))
    # Pre-seed 59 minutes of prior conversation on today's day_key.
    safety.health.record_turn(59 * 60_000)

    core = PersonaCore(
        persona_name="renee",
        config_dir=ROOT / "configs",
        state_dir=tmp_path / "state",
        router=FakeRouter(response_text="I was about to tell a long story."),
        memory_store=None,
        safety_layer=safety,
    )

    result = core.respond("hey", history=[])
    assert result.cap_tripped is True
    assert result.text == "That's the day. I'll be here tomorrow."
    assert "cap_tripped" in result.filters.hits
    assert safety.bridge_allowed_now() is False


# ---------------------------------------------------------------------------
# bridge gating
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.closed = False

    async def send(self, data: Any) -> None:
        self.sent.append(data if isinstance(data, str) else data.decode("utf-8"))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeOrchestrator:
    """Matches the slice of the orchestrator surface the bridge touches."""

    def __init__(self) -> None:
        self.feed_audio_calls = 0
        self.registered = False
        self._end_event: asyncio.Event | None = None

    async def feed_audio(self, pcm: bytes) -> None:
        self.feed_audio_calls += 1

    async def tts_output_stream(self):
        # Park forever until the connection closes.
        await asyncio.Event().wait()
        if False:
            yield b""

    def register_transcript_listener(self, conn_id: Any, cb) -> Any:
        self.registered = True

        def _remove() -> None:
            self.registered = False

        return _remove

    def install_session_end_event(self, event: asyncio.Event) -> None:
        self._end_event = event

    def clear_session_end_event(self) -> None:
        self._end_event = None


def test_bridge_refuses_connection_when_cap_cooldown_active(tmp_path: Path):
    """The bridge's first action on accept is to ask the safety layer
    whether new connections are allowed. If not, it sends the farewell as
    JSON and closes with code 1008."""
    safety = _safety_layer(tmp_path, cap_minutes=1, cooldown_minutes=60)
    # Trip the cap.
    safety.record_turn_duration(120 * 60_000)
    assert safety.bridge_allowed_now() is False

    bridge = CloudAudioBridge(_FakeOrchestrator(), safety_layer=safety)
    ws = _FakeWebSocket()

    asyncio.run(bridge.handle_client(ws))

    assert ws.closed is True
    assert ws.close_code == 1008
    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload["type"] == "bridge_unavailable"
    assert payload["reason"] == "daily_cap_cooldown"
    assert payload["message"] == "That's the day. I'll be here tomorrow."


def test_bridge_session_end_event_closes_active_connection(tmp_path: Path):
    """When the orchestrator sets the session-end event, the bridge closes
    the socket even if the client is still streaming frames."""
    safety = _safety_layer(tmp_path, cap_minutes=0)  # cap disabled for gate
    orch = _FakeOrchestrator()
    bridge = CloudAudioBridge(orch, safety_layer=safety)
    ws = _FakeWebSocket()

    async def _run() -> None:
        # An async iterable the bridge's _receive_audio can consume as "no
        # frames arrived before the end event fired" -- park forever.
        async def _never_yield():
            await asyncio.Event().wait()
            if False:
                yield b""

        # Simple async iterator: patch ws into an "async for" target.
        class _AsyncIter:
            def __aiter__(self) -> "_AsyncIter":
                return self

            async def __anext__(self) -> bytes:
                await asyncio.Event().wait()
                raise StopAsyncIteration

        ws.__aiter__ = _AsyncIter().__aiter__  # type: ignore[attr-defined]
        handle = asyncio.create_task(bridge.handle_client(ws))
        # Give the bridge a tick to register the event, then trip it.
        for _ in range(10):
            await asyncio.sleep(0)
            if orch._end_event is not None:
                break
        assert orch._end_event is not None
        orch._end_event.set()
        await asyncio.wait_for(handle, timeout=2.0)

    asyncio.run(_run())
    assert ws.closed is True
    assert ws.close_code == 1000
