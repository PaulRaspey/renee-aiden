"""Tests for UAHP task-failure certificates (patch 2)."""
from __future__ import annotations

from src.identity.uahp_identity import create_identity
from src.uahp.task_failure import (
    TaskFailureCertificate,
    issue_task_failure_certificate,
    verify_task_failure_certificate,
)


def test_roundtrip_signs_and_verifies():
    ident = create_identity("renee_voice")
    cert = issue_task_failure_certificate(
        ident,
        task_id="tts-synthesize-1",
        error_message="CUDA out of memory on rank 0",
        error_code="OOM",
        metadata={"gpu": "T400"},
    )
    assert verify_task_failure_certificate(ident, cert) is True
    assert cert.agent_id == "renee_voice"
    assert cert.task_id == "tts-synthesize-1"
    assert cert.error_message == "CUDA out of memory on rank 0"
    assert cert.error_code == "OOM"
    assert cert.metadata == {"gpu": "T400"}
    assert cert.cert_id.startswith("tfail-")


def test_tamper_on_error_message_rejected():
    ident = create_identity("renee_voice")
    cert = issue_task_failure_certificate(ident, task_id="t", error_message="real")
    tampered = TaskFailureCertificate(
        **{**cert.__dict__, "error_message": "bogus explanation"}
    )
    assert verify_task_failure_certificate(ident, tampered) is False


def test_tamper_on_task_id_rejected():
    ident = create_identity("renee_ears")
    cert = issue_task_failure_certificate(
        ident, task_id="asr-1", error_message="audio too short"
    )
    tampered = TaskFailureCertificate(**{**cert.__dict__, "task_id": "asr-2"})
    assert verify_task_failure_certificate(ident, tampered) is False


def test_cross_agent_forgery_rejected():
    alice = create_identity("alice")
    bob = create_identity("bob")
    cert_alice = issue_task_failure_certificate(
        alice, task_id="t", error_message="boom"
    )
    assert verify_task_failure_certificate(bob, cert_alice) is False


def test_custom_error_code_preserved():
    ident = create_identity("renee_memory")
    cert = issue_task_failure_certificate(
        ident,
        task_id="retrieve-123",
        error_message="faiss index corrupt",
        error_code="INDEX_CORRUPT",
    )
    assert cert.error_code == "INDEX_CORRUPT"
    d = cert.to_dict()
    assert d["error_code"] == "INDEX_CORRUPT"
    assert verify_task_failure_certificate(ident, cert) is True


def test_default_error_code_is_unknown():
    ident = create_identity("renee_mood")
    cert = issue_task_failure_certificate(ident, task_id="t", error_message="e")
    assert cert.error_code == "UNKNOWN"
    assert verify_task_failure_certificate(ident, cert) is True
