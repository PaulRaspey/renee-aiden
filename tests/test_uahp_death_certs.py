"""Tests for UAHP death certificates (patch 1)."""
from __future__ import annotations

from src.identity.uahp_identity import create_identity
from src.uahp.death_certs import (
    DeathCause,
    DeathCertificate,
    issue_death_certificate,
    verify_death_certificate,
)


def test_roundtrip_signs_and_verifies():
    ident = create_identity("renee_persona")
    cert = issue_death_certificate(
        ident,
        task_id="task-42",
        cause=DeathCause.HEARTBEAT_TIMEOUT,
        last_receipt_id="receipt-abc",
        metadata={"note": "watchdog tripped"},
    )
    assert verify_death_certificate(ident, cert) is True
    assert cert.agent_id == "renee_persona"
    assert cert.task_id == "task-42"
    assert cert.cause is DeathCause.HEARTBEAT_TIMEOUT
    assert cert.last_receipt_id == "receipt-abc"
    assert cert.metadata == {"note": "watchdog tripped"}
    assert cert.death_id.startswith("death-")


def test_tamper_on_task_id_rejected():
    ident = create_identity("renee_voice")
    cert = issue_death_certificate(ident, task_id="task-A", cause=DeathCause.NATURAL)
    tampered = DeathCertificate(**{**cert.__dict__, "task_id": "task-B"})
    assert verify_death_certificate(ident, tampered) is False


def test_tamper_on_cause_rejected():
    ident = create_identity("renee_voice")
    cert = issue_death_certificate(ident, task_id="t", cause=DeathCause.NATURAL)
    tampered = DeathCertificate(**{**cert.__dict__, "cause": DeathCause.SEGFAULT})
    assert verify_death_certificate(ident, tampered) is False


def test_cross_agent_forgery_rejected():
    alice = create_identity("alice")
    bob = create_identity("bob")
    cert_alice = issue_death_certificate(alice, task_id="t", cause=DeathCause.OOM)
    # Claim the cert is Bob's: the signature was made by Alice, so Bob won't verify.
    assert verify_death_certificate(bob, cert_alice) is False


def test_all_death_causes_produce_valid_certificates():
    ident = create_identity("renee_memory")
    for cause in DeathCause:
        cert = issue_death_certificate(ident, task_id=f"t-{cause.value}", cause=cause)
        assert verify_death_certificate(ident, cert) is True, f"failed for {cause}"
        assert cert.cause is cause


def test_backward_compatibility_defaults():
    ident = create_identity("legacy_agent")
    cert = issue_death_certificate(ident)
    assert cert.task_id == "unknown"
    assert cert.cause is DeathCause.NATURAL
    assert cert.last_receipt_id is None
    assert cert.metadata == {}
    assert verify_death_certificate(ident, cert) is True


def test_to_dict_contains_all_fields_and_roundtrips_signature():
    ident = create_identity("renee_mood")
    cert = issue_death_certificate(
        ident, task_id="t", cause=DeathCause.SUPERVISOR_TERMINATED
    )
    d = cert.to_dict()
    assert d["agent_id"] == "renee_mood"
    assert d["cause"] == "supervisor_terminated"
    assert d["task_id"] == "t"
    assert d["signature"] == cert.signature
