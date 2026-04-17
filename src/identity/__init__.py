"""UAHP-native identity for Renée/Aiden agents."""
from .uahp_identity import (
    AgentIdentity,
    CompletionReceipt,
    ReneeIdentityManager,
    create_identity,
    load_or_create,
    sign_receipt,
    verify_receipt,
)

__all__ = [
    "AgentIdentity",
    "CompletionReceipt",
    "ReneeIdentityManager",
    "create_identity",
    "load_or_create",
    "sign_receipt",
    "verify_receipt",
]
