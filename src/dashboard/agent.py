"""Dashboard UAHP agent identity.

The dashboard is a separate UAHP agent with its own keypair so every
config change it signs off on can be verified independently of the
persona core. Same identity-manager pattern the persona core uses.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..identity import ReneeIdentityManager, sign_receipt, CompletionReceipt


@dataclass
class DashboardActionReceipt:
    """Thin wrapper around CompletionReceipt for the dashboard surface."""
    receipt_id: str
    field: str
    signed_at: float
    signature_prefix: str


class DashboardAgent:
    """Provides signed action receipts for dashboard writes."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.identity_manager = ReneeIdentityManager(self.state_dir)
        self.identity = self.identity_manager.get(
            "dashboard_agent",
            metadata={"agent": "dashboard"},
        )

    @property
    def agent_id(self) -> str:
        return self.identity.agent_id

    def sign_action(
        self,
        *,
        field: str,
        old_value,
        new_value,
        confirmed: bool,
        actor: str = "pj",
    ) -> CompletionReceipt:
        now_ms = int(time.time() * 1000)
        return sign_receipt(
            self.identity,
            task_id=f"dash-{now_ms}-{field}",
            action="dashboard.write",
            duration_ms=0.0,
            success=True,
            input_data={"field": field, "old_value": old_value, "actor": actor},
            output_data={"new_value": new_value, "confirmed": confirmed},
            metadata={"actor": actor},
        )
