"""Tests for src.client.memory_bridge_client (#45)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib import error as urllib_error

import pytest

from src.client.memory_bridge_client import (
    HandoffPayload,
    MemoryBridgeClient,
    build_session_handoff,
)


def test_from_env_returns_none_when_vars_missing(monkeypatch):
    monkeypatch.delenv("MEMORY_BRIDGE_URL", raising=False)
    monkeypatch.delenv("MEMORY_BRIDGE_TOKEN", raising=False)
    assert MemoryBridgeClient.from_env() is None


def test_from_env_returns_none_when_only_url_set(monkeypatch):
    monkeypatch.setenv("MEMORY_BRIDGE_URL", "https://x")
    monkeypatch.delenv("MEMORY_BRIDGE_TOKEN", raising=False)
    assert MemoryBridgeClient.from_env() is None


def test_from_env_returns_client_when_both_set(monkeypatch):
    monkeypatch.setenv("MEMORY_BRIDGE_URL", "https://mb.example/")
    monkeypatch.setenv("MEMORY_BRIDGE_TOKEN", "tok")
    c = MemoryBridgeClient.from_env()
    assert c is not None
    assert c.base_url == "https://mb.example"  # trailing slash stripped
    assert c.token == "tok"


def test_handoff_payload_to_dict_strips_none():
    p = HandoffPayload(
        thread_name="x", session_summary="y",
        active_work=None, do_not_forget=["a"],
    )
    d = p.to_dict()
    assert d["thread_name"] == "x"
    assert d["session_summary"] == "y"
    assert "active_work" not in d
    assert d["do_not_forget"] == ["a"]


def test_publish_posts_with_bearer_token():
    client = MemoryBridgeClient("https://mb.example", "secret-token")
    payload = HandoffPayload(thread_name="t", session_summary="s")

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({"handoff_id": "h-123"}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["headers"] = dict(req.headers)
        captured["data"] = req.data
        return FakeResp()

    with patch("src.client.memory_bridge_client.urllib_request.urlopen", side_effect=fake_urlopen):
        result = client.publish(payload)
    assert result == {"handoff_id": "h-123"}
    assert captured["url"] == "https://mb.example/v1/handoffs"
    assert captured["method"] == "POST"
    # urllib title-cases header keys
    assert captured["headers"].get("Authorization") == "Bearer secret-token"
    body = json.loads(captured["data"].decode("utf-8"))
    assert body["thread_name"] == "t"


def test_publish_returns_none_on_http_error():
    client = MemoryBridgeClient("https://mb", "tok")
    err = urllib_error.HTTPError("u", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
    err.read = lambda: b"bridge sleeping"
    with patch("src.client.memory_bridge_client.urllib_request.urlopen", side_effect=err):
        assert client.publish(HandoffPayload(thread_name="t", session_summary="s")) is None


def test_publish_returns_none_on_url_error():
    client = MemoryBridgeClient("https://mb", "tok")
    with patch(
        "src.client.memory_bridge_client.urllib_request.urlopen",
        side_effect=urllib_error.URLError("net down"),
    ):
        assert client.publish(HandoffPayload(thread_name="t", session_summary="s")) is None


def test_build_session_handoff_minimal():
    p = build_session_handoff(thread_name="renee-voice")
    assert p.thread_name == "renee-voice"
    assert "Renée voice session ended" in p.session_summary
    # No topic/pod/cost given -> next prompt has summary only
    assert "Carry forward" in (p.next_session_prompt or "")


def test_build_session_handoff_with_all_signals():
    p = build_session_handoff(
        thread_name="renee-voice",
        topic="memory consolidation Part 3",
        pod_id="pod-x",
        session_dir="/tmp/sessions/2026-05-03",
        cost_summary={"uptime_minutes": 30.0, "gpu_type": "A100", "session_usd": 0.75},
    )
    assert "Topic: memory consolidation Part 3" in p.session_summary
    assert "pod-x" in p.session_summary
    assert "30.0" in p.session_summary or "30" in p.session_summary
    assert p.do_not_forget is not None
    assert any("triage" in line for line in p.do_not_forget)
    assert "Topic" in (p.next_session_prompt or "")
