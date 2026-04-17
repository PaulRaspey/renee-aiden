"""Tests for scripts.cloud_startup — phase ordering and factory injection."""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


cloud_startup = importlib.import_module("cloud_startup")


class FakeOrchestrator:
    def __init__(self):
        self.called = []

    def text_turn(self, text: str):
        self.called.append(text)
        return SimpleNamespace(text="fake")


class FakeBridge:
    def __init__(self, orch, idle):
        self.orch = orch
        self.idle = idle
        self.started = False

    async def start(self, **kwargs):
        self.started = True


class FakeIdleWatcher:
    def __init__(self, seconds):
        self.seconds = seconds


def test_startup_runs_all_phases_with_injected_factories(tmp_path, monkeypatch):
    # Point workspace paths at tmp_path so health checks pass on the dev box.
    monkeypatch.setattr(cloud_startup, "WORKSPACE", tmp_path)
    monkeypatch.setattr(cloud_startup, "MODELS", tmp_path / "models")
    monkeypatch.setattr(cloud_startup, "STATE", tmp_path / "state")
    monkeypatch.setattr(cloud_startup, "DEPLOY_CONFIG", ROOT / "configs" / "deployment.yaml")

    orch = FakeOrchestrator()
    bridges: list[FakeBridge] = []

    def orch_factory():
        return orch

    def bridge_factory(o, i):
        b = FakeBridge(o, i)
        bridges.append(b)
        return b

    def idle_factory(seconds):
        return FakeIdleWatcher(seconds)

    result = asyncio.run(
        cloud_startup.startup(
            orchestrator_factory=orch_factory,
            bridge_factory=bridge_factory,
            idle_watcher_factory=idle_factory,
        )
    )
    assert result.ok is True
    assert result.elapsed_s >= 0
    # Self-test executed the orchestrator at least once.
    assert orch.called
    # Bridge factory was invoked with the orchestrator + idle watcher.
    assert bridges and bridges[0].orch is orch


def test_startup_records_error_when_bridge_start_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_startup, "WORKSPACE", tmp_path)
    monkeypatch.setattr(cloud_startup, "MODELS", tmp_path / "models")
    monkeypatch.setattr(cloud_startup, "STATE", tmp_path / "state")
    monkeypatch.setattr(cloud_startup, "DEPLOY_CONFIG", ROOT / "configs" / "deployment.yaml")

    class BadBridge:
        def __init__(self, o, i):
            pass

        async def start(self, **kwargs):
            raise RuntimeError("simulated bridge failure")

    def bridge_factory(o, i):
        b = BadBridge(o, i)
        # Wrap start so it raises inside orchestration when awaited.
        return b

    # Since the current startup() short-circuits bridge_factory (no start call
    # when factory supplied), we use the no-factory path to exercise the try/
    # except around bridge.start(). Fake out the real CloudAudioBridge import.
    import src.server.audio_bridge as real
    class FakeRealBridge:
        def __init__(self, *a, **k):
            pass

        async def start(self, **kwargs):
            raise RuntimeError("kapow")
    monkeypatch.setattr(real, "CloudAudioBridge", FakeRealBridge)

    orch = FakeOrchestrator()
    result = asyncio.run(
        cloud_startup.startup(
            orchestrator_factory=lambda: orch,
            idle_watcher_factory=lambda s: FakeIdleWatcher(s),
        )
    )
    # Error tracked; result.ok False.
    assert result.ok is False
    assert any("audio_bridge_start" in e for e in result.errors)


def test_startup_health_checks_require_workspace(monkeypatch):
    monkeypatch.setattr(cloud_startup, "WORKSPACE", Path("/no-such-path-xyz-123"))
    import pytest
    with pytest.raises(RuntimeError):
        cloud_startup._health_checks()
