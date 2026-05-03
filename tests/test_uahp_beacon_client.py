"""Unit tests for src.uahp.beacon_client.

Mocks all HTTP — no live Beacon needed. Verifies:
  - register persists credentials
  - heartbeat re-uses persisted credentials across "restarts"
  - 409 Conflict (dead agent) clears credentials
  - missing BEACON_URL produces None client
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch
from urllib import error as urllib_error

import pytest

from src.uahp.beacon_client import (
    BeaconClient,
    BeaconCredentials,
    _load_credentials,
)


def test_from_env_returns_none_without_url(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BEACON_URL", raising=False)
    assert BeaconClient.from_env(tmp_path) is None


def test_from_env_loads_persisted_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BEACON_URL", "https://beacon.example.com")
    creds_path = tmp_path / "beacon_credentials.json"
    creds_path.write_text(json.dumps({
        "agent_id": "agt_persisted",
        "api_key": "bk_secret",
        "base_url": "https://beacon.example.com",
    }))
    client = BeaconClient.from_env(tmp_path)
    assert client is not None
    assert client.credentials is not None
    assert client.credentials.agent_id == "agt_persisted"


def test_from_env_drops_credentials_when_url_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BEACON_URL", "https://new-beacon.example.com")
    creds_path = tmp_path / "beacon_credentials.json"
    creds_path.write_text(json.dumps({
        "agent_id": "agt_old",
        "api_key": "bk_secret",
        "base_url": "https://old-beacon.example.com",
    }))
    client = BeaconClient.from_env(tmp_path)
    assert client is not None
    assert client.credentials is None  # stale creds discarded


@pytest.mark.asyncio
async def test_ensure_registered_persists_credentials(tmp_path: Path):
    client = BeaconClient("https://beacon.example.com", tmp_path)
    fake_response = {"agent_id": "agt_new", "api_key": "bk_new", "registered_at": "now"}
    with patch("src.uahp.beacon_client._http_post", return_value=fake_response) as mock_post:
        creds = await client.ensure_registered(name="renee_test", interval_seconds=15)
    assert creds.agent_id == "agt_new"
    assert creds.api_key == "bk_new"
    # Was persisted to disk
    persisted = _load_credentials(tmp_path)
    assert persisted is not None
    assert persisted.agent_id == "agt_new"
    # Register URL was correct
    assert mock_post.call_args.args[0] == "https://beacon.example.com/v1/agents/register"


@pytest.mark.asyncio
async def test_ensure_registered_skipped_when_creds_exist(tmp_path: Path):
    creds = BeaconCredentials(agent_id="agt_x", api_key="bk_x", base_url="https://b.example")
    client = BeaconClient("https://b.example", tmp_path, credentials=creds)
    with patch("src.uahp.beacon_client._http_post") as mock_post:
        result = await client.ensure_registered()
    assert result is creds
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_includes_bearer_token(tmp_path: Path):
    creds = BeaconCredentials(agent_id="agt_x", api_key="bk_secret", base_url="https://b.example")
    client = BeaconClient("https://b.example", tmp_path, credentials=creds)
    with patch("src.uahp.beacon_client._http_post", return_value={"received_at": "t"}) as mock_post:
        await client.heartbeat(status_note="alive")
    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer bk_secret"
    assert mock_post.call_args.args[0] == "https://b.example/v1/agents/agt_x/heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_409_clears_credentials(tmp_path: Path):
    creds = BeaconCredentials(agent_id="agt_dead", api_key="bk_x", base_url="https://b.example")
    client = BeaconClient("https://b.example", tmp_path, credentials=creds)
    # Persist so we can verify deletion
    (tmp_path / "beacon_credentials.json").write_text(json.dumps(creds.to_dict()))
    err = urllib_error.HTTPError("u", 409, "Conflict", {}, None)  # type: ignore[arg-type]
    with patch("src.uahp.beacon_client._http_post", side_effect=err):
        result = await client.heartbeat()
    assert result is None
    assert client.credentials is None
    assert not (tmp_path / "beacon_credentials.json").exists()


@pytest.mark.asyncio
async def test_heartbeat_transport_error_swallowed(tmp_path: Path):
    creds = BeaconCredentials(agent_id="agt_x", api_key="bk_x", base_url="https://b.example")
    client = BeaconClient("https://b.example", tmp_path, credentials=creds)
    with patch("src.uahp.beacon_client._http_post", side_effect=urllib_error.URLError("net down")):
        result = await client.heartbeat()
    assert result is None
    # Credentials are kept — transport error doesn't mean we're dead
    assert client.credentials is not None


@pytest.mark.asyncio
async def test_run_heartbeat_loop_stops_on_signal(tmp_path: Path):
    creds = BeaconCredentials(agent_id="agt_x", api_key="bk_x", base_url="https://b.example")
    client = BeaconClient("https://b.example", tmp_path, credentials=creds)
    client._heartbeat_interval_s = 1
    call_count = 0

    def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return {}

    with patch("src.uahp.beacon_client._http_post", side_effect=fake_post):
        loop_task = asyncio.create_task(client.run_heartbeat_loop())
        # Give the loop a tick to send one heartbeat, then stop
        await asyncio.sleep(0.05)
        client.stop()
        await asyncio.wait_for(loop_task, timeout=2.0)
    assert call_count >= 1


@pytest.mark.asyncio
async def test_run_heartbeat_loop_noop_without_credentials(tmp_path: Path):
    client = BeaconClient("https://b.example", tmp_path, credentials=None)
    with patch("src.uahp.beacon_client._http_post") as mock_post:
        await asyncio.wait_for(client.run_heartbeat_loop(), timeout=1.0)
    mock_post.assert_not_called()
