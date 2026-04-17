"""UAHP identity: create, sign, verify, persist, and cross-verify receipts."""
from pathlib import Path

import pytest

from src.identity import (
    AgentIdentity,
    ReneeIdentityManager,
    load_or_create,
    sign_receipt,
    verify_receipt,
)
from src.identity.uahp_identity import create_identity


def test_sign_and_verify_roundtrip():
    identity = create_identity("test-agent")
    msg = "hello world"
    sig = identity.sign(msg)
    assert identity.verify(msg, sig)
    assert not identity.verify("tampered", sig)


def test_receipt_roundtrip():
    identity = create_identity("renee_persona")
    receipt = sign_receipt(
        identity,
        task_id="t1",
        action="persona.respond",
        duration_ms=123.4,
        success=True,
        input_data={"user": "hi"},
        output_data={"text": "hey"},
    )
    assert verify_receipt(identity, receipt)
    # tampering breaks verification
    receipt.duration_ms = 9999
    assert not verify_receipt(identity, receipt)


def test_load_or_create_persists(tmp_path: Path):
    id1 = load_or_create("renee_persona", tmp_path)
    id2 = load_or_create("renee_persona", tmp_path)
    assert id1.signing_key == id2.signing_key
    assert id1.public_hash == id2.public_hash


def test_bootstrap_all(tmp_path: Path):
    mgr = ReneeIdentityManager(tmp_path)
    ids = mgr.bootstrap_all()
    assert "renee_persona" in ids
    assert "aiden_persona" in ids
    # all distinct keys
    keys = {i.signing_key for i in ids.values()}
    assert len(keys) == len(ids)
