"""Receiver for Beacon agent.death webhooks.

The Beacon liveness service (PaulRaspey/Beacon-Prompt) signs death
certificates with Ed25519 and POSTs them to subscribers when an agent's
heartbeat goes silent past `interval + grace`. This module verifies the
HMAC signature on each delivery, parses the certificate body, and
appends the result to a JSONL journal so the dashboard's Health tab and
the M15 audit trail can surface dead-agent events.

The receiver does NOT verify the Ed25519 signature on the cert body
itself — that's the job of any party that wants cryptographic proof.
The HMAC envelope is enough to confirm the POST came from a beacon we
trust (the public key is configured locally), and the cert body is
preserved verbatim in the journal for downstream verification.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger("renee.beacon_receiver")


DEFAULT_DEATHS_JOURNAL = Path("state") / "beacon_deaths.jsonl"


@dataclass
class WebhookResult:
    ok: bool
    error: Optional[str] = None
    certificate: Optional[dict] = None
    delivery: dict = field(default_factory=dict)


def _expected_public_key() -> Optional[str]:
    """Source the trusted Beacon public key.

    Preference order: BEACON_PUBLIC_KEY env, then state/beacon_public_key.b64
    (stored once during setup so the receiver doesn't depend on env wiring).
    Returns None when neither is configured — the receiver then refuses
    every webhook so an unconfigured installation can't be silently
    fed bogus deaths."""
    env = os.environ.get("BEACON_PUBLIC_KEY", "").strip()
    if env:
        return env
    f = Path("state") / "beacon_public_key.b64"
    if f.exists():
        return f.read_text(encoding="utf-8").strip() or None
    return None


def _verify_signature(raw_body: bytes, header_value: str, public_key: str) -> bool:
    """HMAC-SHA256 verification matching the sender's signPayload().

    Header is ``sha256=<hex>``; we split on the first '=' to be lenient
    against future scheme prefixes. Compare via constant-time hmac.compare_digest
    to avoid timing leaks."""
    if not header_value:
        return False
    if "=" not in header_value:
        return False
    _, _, expected_hex = header_value.partition("=")
    expected_hex = expected_hex.strip()
    if not expected_hex:
        return False
    want = hmac.new(
        public_key.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(want, expected_hex)


def receive_webhook(
    *,
    raw_body: bytes,
    signature_header: str,
    event_header: str = "",
    journal_path: Optional[Path] = None,
    public_key: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
) -> WebhookResult:
    """Verify + persist a Beacon webhook POST.

    Returns a WebhookResult that the caller turns into an HTTP response.
    journal_path is injectable for tests; defaults to DEFAULT_DEATHS_JOURNAL.
    """
    key = public_key or _expected_public_key()
    if not key:
        return WebhookResult(ok=False, error="no BEACON_PUBLIC_KEY configured")
    if not _verify_signature(raw_body, signature_header, key):
        return WebhookResult(ok=False, error="signature mismatch")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        return WebhookResult(ok=False, error=f"invalid JSON: {e}")
    if not isinstance(payload, dict):
        return WebhookResult(ok=False, error="payload is not an object")
    if event_header and payload.get("event") and payload["event"] != event_header:
        # Defensive: header and body event must agree if both present
        return WebhookResult(
            ok=False,
            error=f"event mismatch: header={event_header} body={payload.get('event')}",
        )
    cert = payload.get("certificate")
    if not isinstance(cert, dict):
        return WebhookResult(ok=False, error="certificate field missing or not an object")

    when = (now or _dt.datetime.now(_dt.timezone.utc)).isoformat()
    record = {
        "received_at": when,
        "event": payload.get("event") or event_header or "agent.death",
        "certificate": cert,
        "server_public_key": payload.get("server_public_key"),
    }
    target = Path(journal_path) if journal_path else DEFAULT_DEATHS_JOURNAL
    target.parent.mkdir(parents=True, exist_ok=True)
    # Append-only JSONL — one death per line. Crash-safe enough for our scale;
    # the cert is signed so re-reading is verifiable independently.
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")

    logger.info(
        "beacon webhook accepted: agent_id=%s cert_id=%s",
        cert.get("agent_id"), cert.get("certificate_id"),
    )
    return WebhookResult(ok=True, certificate=cert, delivery=record)


def list_recent_deaths(
    *,
    limit: int = 50,
    journal_path: Optional[Path] = None,
) -> list[dict]:
    """Read the last ``limit`` entries from the deaths journal, newest
    first. Returns an empty list when the journal is missing."""
    target = Path(journal_path) if journal_path else DEFAULT_DEATHS_JOURNAL
    if not target.exists():
        return []
    out: list[dict] = []
    with target.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    out.reverse()
    return out[:limit]


__all__ = [
    "DEFAULT_DEATHS_JOURNAL",
    "WebhookResult",
    "receive_webhook",
    "list_recent_deaths",
]
