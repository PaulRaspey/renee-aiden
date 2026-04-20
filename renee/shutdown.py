"""`python -m renee.shutdown --confirm`

Graceful, authenticated termination of the Renée stack. Writes a signed
death certificate for each persona agent into
state/identities/death_certificates/, flushes mood state, and exits with
code 0 on success.

This is the companion to `python -m renee sleep` (which stops billing on
the pod). Shutdown is the UAHP-native path; sleep is the RunPod-native
path. They compose: `renee.shutdown` then `renee sleep`.

Safety rails:
- Requires `--confirm` to execute. Absent, prints the plan and exits 2.
- Does NOT wipe memory. Memory DB survives by design so `renee wake`
  can restore continuity. Use `scripts/export_memory.py` first if you
  want a backup, and `scripts/wipe_state.py` (NOT shipped yet) if you
  want a clean start.
- Idempotent: a second run simply adds another death certificate row.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.identity.uahp_identity import (  # noqa: E402
    ReneeIdentityManager,
    sign_receipt,
)


DEATH_CERT_DIR = "death_certificates"


def _persona_agent_names(persona: str) -> list[str]:
    prefix = f"{persona}_"
    return [n for n in ReneeIdentityManager.AGENT_NAMES if n.startswith(prefix)]


def issue_death_certificate(state_dir: Path, persona: str) -> dict:
    """Sign one death certificate per persona agent. Returns a dict with
    the certificate ids and the state paths that were touched."""
    mgr = ReneeIdentityManager(state_dir)
    out_dir = state_dir / "identities" / DEATH_CERT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    certs: list[dict] = []
    for agent in _persona_agent_names(persona):
        identity = mgr.get(agent)
        receipt = sign_receipt(
            identity,
            task_id=f"shutdown-{int(time.time()*1000)}",
            action="agent.death",
            duration_ms=0.0,
            success=True,
            input_data={"agent_id": agent, "persona": persona},
            output_data={"shutdown_at": time.time()},
            metadata={"kind": "death_certificate"},
        )
        cert = {
            "agent_id": agent,
            "persona": persona,
            "receipt": asdict(receipt),
        }
        out_path = out_dir / f"{agent}-{receipt.receipt_id}.json"
        out_path.write_text(
            json.dumps(cert, indent=2, default=str), encoding="utf-8",
        )
        certs.append({"agent": agent, "path": str(out_path), "receipt_id": receipt.receipt_id})
    return {"persona": persona, "certificates": certs, "count": len(certs)}


def freeze_mood(state_dir: Path, persona: str) -> dict:
    """Take a final mood snapshot so cold-wake has a known-good last
    state. Idempotent and safe to call when no mood DB exists yet.
    Returns the written row or an explanation."""
    db_path = state_dir / f"{persona}_mood.db"
    if not db_path.exists():
        return {"persona": persona, "mood_db": str(db_path), "status": "absent"}
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT energy,warmth,playfulness,focus,patience,curiosity,last_updated "
            "FROM mood WHERE id=1"
        ).fetchone()
        if row is None:
            return {"persona": persona, "mood_db": str(db_path), "status": "empty"}
        frozen = {
            "energy": row[0],
            "warmth": row[1],
            "playfulness": row[2],
            "focus": row[3],
            "patience": row[4],
            "curiosity": row[5],
            "last_updated": row[6],
            "frozen_at": time.time(),
        }
        c.execute(
            "INSERT INTO mood_log (ts,event,delta_json,state_json) "
            "VALUES (?, 'shutdown_freeze', ?, ?)",
            (
                frozen["frozen_at"],
                json.dumps({"cause": "shutdown"}),
                json.dumps(frozen),
            ),
        )
    return {"persona": persona, "mood_db": str(db_path), "status": "frozen", "snapshot": frozen}


def shutdown(*, state_dir: Path, persona: str, confirmed: bool) -> dict:
    if not confirmed:
        return {
            "dry_run": True,
            "persona": persona,
            "state_dir": str(state_dir),
            "would_issue": _persona_agent_names(persona),
            "note": "pass --confirm to actually shut down",
        }
    certs = issue_death_certificate(state_dir, persona)
    frozen = freeze_mood(state_dir, persona)
    return {
        "dry_run": False,
        "persona": persona,
        "state_dir": str(state_dir),
        "death_certificates": certs,
        "mood_freeze": frozen,
        "completed_at": time.time(),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="renee.shutdown")
    parser.add_argument("--persona", default="renee")
    parser.add_argument("--state-dir", default=str(REPO_ROOT / "state"))
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually perform the shutdown; without this, print the plan",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = shutdown(
        state_dir=Path(args.state_dir),
        persona=args.persona,
        confirmed=bool(args.confirm),
    )
    print(json.dumps(result, indent=2, default=str))
    if result.get("dry_run"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
