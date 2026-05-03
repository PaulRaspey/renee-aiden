"""HTTP client for the Memory Bridge handoff API.

The Memory Bridge (separate Replit project at PaulRaspey/Memory-Bridge)
captures structured session context so the *next* Claude session has the
last session's context, decisions, and open questions on hand without
Paul having to re-paste a summary.

This client wraps a single POST /v1/handoffs call so the launcher's
session-end path can publish a handoff automatically. If MEMORY_BRIDGE_URL
or MEMORY_BRIDGE_TOKEN is unset, all calls are no-ops — Renée's session
ends fine without a handoff being captured.

Failure modes (Memory Bridge unreachable, 5xx, malformed response) are
caught and logged; they never propagate up to crash the session-end
shutdown path.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib import request as urllib_request
from urllib import error as urllib_error


logger = logging.getLogger("renee.memory_bridge")


@dataclass
class HandoffPayload:
    """Subset of the Memory Bridge HandoffCreate schema we actually populate
    from a Renée session. Optional fields default to None / empty so we
    can build the payload incrementally as evidence becomes available."""
    thread_name: str
    session_summary: str
    active_work: Optional[list[dict]] = None
    decisions_made: Optional[list[dict]] = None
    open_questions: Optional[list[dict]] = None
    do_not_forget: Optional[list[str]] = None
    next_session_prompt: Optional[str] = None
    written_at: Optional[str] = None  # ISO-8601 UTC

    def to_dict(self) -> dict:
        d: dict = {
            "thread_name": self.thread_name,
            "session_summary": self.session_summary,
        }
        if self.active_work is not None:
            d["active_work"] = self.active_work
        if self.decisions_made is not None:
            d["decisions_made"] = self.decisions_made
        if self.open_questions is not None:
            d["open_questions"] = self.open_questions
        if self.do_not_forget is not None:
            d["do_not_forget"] = self.do_not_forget
        if self.next_session_prompt is not None:
            d["next_session_prompt"] = self.next_session_prompt
        if self.written_at is not None:
            d["written_at"] = self.written_at
        return d


class MemoryBridgeClient:
    """Thin POST client for /v1/handoffs. Exists as a class so callers
    can stash the resolved URL + token once at construction and call
    publish() repeatedly during a long process if needed."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    @classmethod
    def from_env(cls) -> Optional["MemoryBridgeClient"]:
        url = os.environ.get("MEMORY_BRIDGE_URL", "").strip()
        token = os.environ.get("MEMORY_BRIDGE_TOKEN", "").strip()
        # Both must be set; otherwise we silently disable.
        if not url or not token:
            return None
        return cls(url, token)

    def publish(self, payload: HandoffPayload, *, timeout: float = 5.0) -> Optional[dict]:
        """POST a handoff. Returns the parsed response dict, or None on
        any error (logged, not raised)."""
        body = json.dumps(payload.to_dict()).encode("utf-8")
        req = urllib_request.Request(
            f"{self.base_url}/v1/handoffs",
            data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib_error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                detail = ""
            logger.warning("Memory Bridge HTTP %s: %s", e.code, detail)
            return None
        except urllib_error.URLError as e:
            logger.warning("Memory Bridge unreachable: %s", e)
            return None
        except Exception as e:
            logger.warning("Memory Bridge publish failed: %r", e)
            return None


# ---------------------------------------------------------------------------
# Builders for the launcher's auto-handoff
# ---------------------------------------------------------------------------


def build_session_handoff(
    *,
    thread_name: str,
    topic: Optional[str] = None,
    sessions_root: Optional[Any] = None,
    session_dir: Optional[Any] = None,
    cost_summary: Optional[dict] = None,
    pod_id: Optional[str] = None,
) -> HandoffPayload:
    """Compose a HandoffPayload from whatever Renée signals are available
    at session end. Each piece is optional; this builder degrades to a
    bare summary when the rest is missing.

    The result is what Memory Bridge stores; the next Claude session sees
    it via /p/<id>/latest in markdown form.
    """
    when = _dt.datetime.now(_dt.timezone.utc).isoformat()
    summary_lines = [f"Renée voice session ended {when}."]
    if topic:
        summary_lines.append(f"Topic: {topic}.")
    if pod_id:
        summary_lines.append(f"Pod: {pod_id}.")
    if cost_summary:
        c = cost_summary
        summary_lines.append(
            f"Pod up {c.get('uptime_minutes', '?')}m on "
            f"{c.get('gpu_type', '?')}; ≈${c.get('session_usd', '?')}."
        )

    do_not_forget: list[str] = []
    if session_dir:
        do_not_forget.append(
            f"Session capture written to {session_dir}; run `python -m renee triage <dir>` for highlights."
        )

    next_prompt_bits = ["Carry forward from the prior Renée voice session:"]
    next_prompt_bits.extend(summary_lines)
    if do_not_forget:
        next_prompt_bits.append("")
        next_prompt_bits.append("Do not forget:")
        next_prompt_bits.extend(f"- {x}" for x in do_not_forget)

    return HandoffPayload(
        thread_name=thread_name,
        session_summary=" ".join(summary_lines),
        do_not_forget=do_not_forget or None,
        next_session_prompt="\n".join(next_prompt_bits),
        written_at=when,
    )


__all__ = [
    "HandoffPayload",
    "MemoryBridgeClient",
    "build_session_handoff",
]
