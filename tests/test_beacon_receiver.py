"""Tests for src.server.beacon_receiver (#46)."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.server.beacon_receiver import (
    list_recent_deaths,
    receive_webhook,
    _verify_signature,
)


_PUBKEY = "ZmFrZS1iZWFjb24tcHViLWtleQ=="  # any consistent string works for HMAC tests


def _sign(body: bytes, key: str = _PUBKEY) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def _sample_payload() -> dict:
    return {
        "event": "agent.death",
        "certificate": {
            "certificate_id": "cert_123",
            "agent_id": "agt_x",
            "agent_name": "renee_orchestrator",
            "issuer": "beacon.localhost",
            "spec_version": "uahp-beacon-v0.1",
        },
        "server_public_key": _PUBKEY,
    }


# ---------------------------------------------------------------------------
# signature verification
# ---------------------------------------------------------------------------


def test_verify_signature_accepts_correct_hmac():
    body = b'{"hello":"world"}'
    sig = _sign(body)
    assert _verify_signature(body, sig, _PUBKEY) is True


def test_verify_signature_rejects_wrong_key():
    body = b'{"hello":"world"}'
    sig = _sign(body, key="other-key")
    assert _verify_signature(body, sig, _PUBKEY) is False


def test_verify_signature_rejects_missing_header():
    assert _verify_signature(b"x", "", _PUBKEY) is False
    assert _verify_signature(b"x", "sha256=", _PUBKEY) is False


def test_verify_signature_rejects_garbage_format():
    assert _verify_signature(b"x", "no-equals", _PUBKEY) is False


# ---------------------------------------------------------------------------
# receive_webhook
# ---------------------------------------------------------------------------


def test_receive_webhook_persists_when_signature_valid(tmp_path: Path):
    body = json.dumps(_sample_payload()).encode("utf-8")
    journal = tmp_path / "deaths.jsonl"
    result = receive_webhook(
        raw_body=body, signature_header=_sign(body),
        event_header="agent.death",
        journal_path=journal, public_key=_PUBKEY,
    )
    assert result.ok is True
    assert result.certificate["certificate_id"] == "cert_123"
    # Journal contains exactly one line with the event + cert
    lines = journal.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "agent.death"
    assert record["certificate"]["agent_id"] == "agt_x"


def test_receive_webhook_appends_multiple(tmp_path: Path):
    journal = tmp_path / "deaths.jsonl"
    for _ in range(3):
        body = json.dumps(_sample_payload()).encode("utf-8")
        receive_webhook(
            raw_body=body, signature_header=_sign(body),
            journal_path=journal, public_key=_PUBKEY,
        )
    lines = journal.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


def test_receive_webhook_refuses_without_public_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BEACON_PUBLIC_KEY", raising=False)
    body = json.dumps(_sample_payload()).encode("utf-8")
    result = receive_webhook(
        raw_body=body, signature_header=_sign(body),
        journal_path=tmp_path / "deaths.jsonl",
        public_key=None,
    )
    assert result.ok is False
    assert "public" in result.error.lower() or "key" in result.error.lower()


def test_receive_webhook_rejects_bad_signature(tmp_path: Path):
    body = json.dumps(_sample_payload()).encode("utf-8")
    result = receive_webhook(
        raw_body=body, signature_header="sha256=deadbeef",
        journal_path=tmp_path / "deaths.jsonl", public_key=_PUBKEY,
    )
    assert result.ok is False
    assert "signature" in result.error.lower()
    # Nothing written to journal on auth failure
    assert not (tmp_path / "deaths.jsonl").exists()


def test_receive_webhook_rejects_invalid_json(tmp_path: Path):
    body = b"this is not json"
    result = receive_webhook(
        raw_body=body, signature_header=_sign(body),
        journal_path=tmp_path / "deaths.jsonl", public_key=_PUBKEY,
    )
    assert result.ok is False
    assert "json" in result.error.lower()


def test_receive_webhook_rejects_event_header_mismatch(tmp_path: Path):
    body = json.dumps(_sample_payload()).encode("utf-8")
    result = receive_webhook(
        raw_body=body, signature_header=_sign(body),
        event_header="agent.something_else",
        journal_path=tmp_path / "deaths.jsonl", public_key=_PUBKEY,
    )
    assert result.ok is False
    assert "event" in result.error.lower()


def test_receive_webhook_requires_certificate_field(tmp_path: Path):
    payload = _sample_payload()
    del payload["certificate"]
    body = json.dumps(payload).encode("utf-8")
    result = receive_webhook(
        raw_body=body, signature_header=_sign(body),
        journal_path=tmp_path / "deaths.jsonl", public_key=_PUBKEY,
    )
    assert result.ok is False
    assert "certificate" in result.error.lower()


# ---------------------------------------------------------------------------
# list_recent_deaths
# ---------------------------------------------------------------------------


def test_list_recent_deaths_returns_empty_when_journal_missing(tmp_path: Path):
    assert list_recent_deaths(journal_path=tmp_path / "missing.jsonl") == []


def test_list_recent_deaths_returns_newest_first(tmp_path: Path):
    journal = tmp_path / "deaths.jsonl"
    for i in range(5):
        body = json.dumps({
            "event": "agent.death",
            "certificate": {"certificate_id": f"cert_{i}", "agent_id": "x"},
            "server_public_key": _PUBKEY,
        }).encode("utf-8")
        receive_webhook(
            raw_body=body, signature_header=_sign(body),
            journal_path=journal, public_key=_PUBKEY,
        )
    rows = list_recent_deaths(limit=3, journal_path=journal)
    assert len(rows) == 3
    # Newest first => cert_4, cert_3, cert_2
    assert [r["certificate"]["certificate_id"] for r in rows] == [
        "cert_4", "cert_3", "cert_2",
    ]


def test_list_recent_deaths_skips_corrupt_lines(tmp_path: Path):
    journal = tmp_path / "deaths.jsonl"
    journal.write_text(
        '{"event":"agent.death","certificate":{"certificate_id":"a"}}\n'
        'corrupt line not json\n'
        '{"event":"agent.death","certificate":{"certificate_id":"b"}}\n',
        encoding="utf-8",
    )
    rows = list_recent_deaths(journal_path=journal)
    assert len(rows) == 2
    assert {r["certificate"]["certificate_id"] for r in rows} == {"a", "b"}
