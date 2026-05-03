"""HTTP client for the UAHP-Beacon liveness service.

Beacon (separate Replit project, `Beacon-Prompt`) tracks agent liveness by
accepting periodic heartbeats and issues a signed Ed25519 death certificate
when an agent stops sending them. This client wraps register + heartbeat for
a Renée-stack agent so `scripts/cloud_startup.py` can register on boot and
emit heartbeats throughout the session.

The agent_id + api_key are persisted to ``<state_dir>/beacon_credentials.json``
so the agent re-uses its identity across pod restarts.

If ``BEACON_URL`` is unset, all calls become no-ops — Renée degrades
gracefully when the liveness service is unavailable. HTTP errors during
the heartbeat loop are logged but do not crash the surrounding voice loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib import request as urllib_request
from urllib import error as urllib_error


logger = logging.getLogger("renee.uahp.beacon_client")


@dataclass
class BeaconCredentials:
    agent_id: str
    api_key: str
    base_url: str

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "api_key": self.api_key, "base_url": self.base_url}

    @classmethod
    def from_dict(cls, data: dict) -> "BeaconCredentials":
        return cls(
            agent_id=str(data["agent_id"]),
            api_key=str(data["api_key"]),
            base_url=str(data["base_url"]),
        )


def _http_post(url: str, body: dict, *, headers: Optional[dict] = None, timeout: float = 10.0) -> dict:
    """Blocking POST. Returns parsed JSON or raises on non-2xx."""
    data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib_request.Request(url, data=data, headers=h, method="POST")
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


class BeaconClient:
    """Register once, then heartbeat at the configured interval.

    Typical use::

        client = BeaconClient.from_env(state_dir=Path("state"))
        if client is not None:
            await client.ensure_registered(name="renee_orchestrator", interval_seconds=30)
            task = asyncio.create_task(client.run_heartbeat_loop())
            ...
            task.cancel()
    """

    def __init__(self, base_url: str, state_dir: Path, *, credentials: Optional[BeaconCredentials] = None):
        self.base_url = base_url.rstrip("/")
        self.state_dir = Path(state_dir)
        self.credentials = credentials
        self._heartbeat_interval_s: int = 30
        self._stopped = asyncio.Event()

    # ------------------------------------------------------------------ env

    @classmethod
    def from_env(cls, state_dir: Path) -> Optional["BeaconClient"]:
        """Build from ``BEACON_URL`` env var, or return None if unset."""
        url = os.environ.get("BEACON_URL", "").strip()
        if not url:
            logger.info("BEACON_URL not set — beacon liveness disabled")
            return None
        creds = _load_credentials(state_dir)
        # If the persisted credentials point at a different beacon, drop
        # them. The new beacon will mint fresh ones on register.
        if creds is not None and creds.base_url.rstrip("/") != url.rstrip("/"):
            logger.info(
                "BEACON_URL changed (%s -> %s) — discarding stale credentials",
                creds.base_url, url,
            )
            creds = None
        return cls(url, state_dir, credentials=creds)

    # -------------------------------------------------------------- register

    async def ensure_registered(
        self,
        *,
        name: str = "renee_orchestrator",
        description: str = "Renée voice orchestrator",
        interval_seconds: int = 30,
        grace_seconds: int = 15,
        metadata: Optional[dict] = None,
    ) -> BeaconCredentials:
        """Register with Beacon if no persisted credentials exist."""
        self._heartbeat_interval_s = interval_seconds
        if self.credentials is not None:
            logger.info(
                "Beacon credentials already present (agent_id=%s); skipping register",
                self.credentials.agent_id,
            )
            return self.credentials
        body = {
            "name": name,
            "description": description,
            "heartbeat_interval_seconds": interval_seconds,
            "grace_period_seconds": grace_seconds,
            "metadata": metadata or {},
        }
        url = f"{self.base_url}/v1/agents/register"
        try:
            resp = await asyncio.to_thread(_http_post, url, body)
        except urllib_error.URLError as e:
            logger.warning("Beacon register failed: %s — liveness disabled this session", e)
            raise
        creds = BeaconCredentials(
            agent_id=str(resp["agent_id"]),
            api_key=str(resp["api_key"]),
            base_url=self.base_url,
        )
        _save_credentials(self.state_dir, creds)
        self.credentials = creds
        logger.info("Registered with Beacon: agent_id=%s", creds.agent_id)
        return creds

    # ------------------------------------------------------------- heartbeat

    async def heartbeat(
        self,
        *,
        status_note: Optional[str] = None,
        metrics: Optional[dict] = None,
    ) -> Optional[dict]:
        """Send a single heartbeat. Returns None on transport error."""
        if self.credentials is None:
            return None
        url = f"{self.base_url}/v1/agents/{self.credentials.agent_id}/heartbeat"
        body: dict[str, Any] = {}
        if status_note is not None:
            body["status_note"] = status_note
        if metrics is not None:
            body["metrics"] = metrics
        headers = {"Authorization": f"Bearer {self.credentials.api_key}"}
        try:
            return await asyncio.to_thread(_http_post, url, body, headers=headers)
        except urllib_error.HTTPError as e:
            # 409 == agent declared dead by reaper. Persisted creds are
            # useless — drop them so the next start re-registers fresh.
            if e.code == 409:
                logger.warning("Beacon reports agent dead — discarding credentials")
                _delete_credentials(self.state_dir)
                self.credentials = None
            else:
                logger.warning("Beacon heartbeat HTTP %s — continuing", e.code)
            return None
        except urllib_error.URLError as e:
            logger.debug("Beacon heartbeat transport error: %s — continuing", e)
            return None

    async def run_heartbeat_loop(self) -> None:
        """Block forever, sending a heartbeat every ``interval_seconds`` until cancelled."""
        if self.credentials is None:
            return
        interval = max(1, self._heartbeat_interval_s)
        logger.info("Beacon heartbeat loop running (interval=%ds)", interval)
        try:
            while not self._stopped.is_set():
                await self.heartbeat()
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass  # expected — keep looping
        finally:
            logger.info("Beacon heartbeat loop stopped")

    def stop(self) -> None:
        self._stopped.set()


# ---------------------------------------------------------------- credentials


CREDS_FILENAME = "beacon_credentials.json"


def _creds_path(state_dir: Path) -> Path:
    return Path(state_dir) / CREDS_FILENAME


def _load_credentials(state_dir: Path) -> Optional[BeaconCredentials]:
    path = _creds_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BeaconCredentials.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Beacon credentials file %s is corrupt (%s) — discarding", path, e)
        return None


def _save_credentials(state_dir: Path, creds: BeaconCredentials) -> None:
    path = _creds_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _delete_credentials(state_dir: Path) -> None:
    path = _creds_path(state_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
