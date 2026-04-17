"""
Idle watcher for cloud deployments (M14).

After `idle_timeout_s` seconds without an `.mark_activity()` call the
watcher fires `on_shutdown()` exactly once. Designed for the RunPod
auto-shutdown path: the audio bridge calls `.mark_activity()` on every
inbound frame, and a scheduler calls `.tick()` periodically. When no
audio has arrived for the full timeout, `on_shutdown` gracefully tears
down the pod.

Clocking is pluggable (`now_fn`) so tests run deterministically.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional


ShutdownFn = Callable[[], None]


class IdleWatcher:
    def __init__(
        self,
        idle_timeout_s: float,
        on_shutdown: ShutdownFn,
        *,
        now_fn: Callable[[], float] = time.monotonic,
    ):
        if idle_timeout_s <= 0:
            raise ValueError("idle_timeout_s must be > 0")
        self.idle_timeout_s = float(idle_timeout_s)
        self._on_shutdown = on_shutdown
        self._now = now_fn
        self._last_activity: float = now_fn()
        self._triggered: bool = False

    # -------------------- api --------------------

    def mark_activity(self) -> None:
        """Bump the last-activity timestamp; reset the triggered latch."""
        self._last_activity = self._now()
        self._triggered = False

    def seconds_idle(self) -> float:
        return max(0.0, self._now() - self._last_activity)

    def should_shutdown(self) -> bool:
        return self.seconds_idle() >= self.idle_timeout_s

    def tick(self) -> bool:
        """
        Call periodically. Returns True if shutdown fired this tick, False
        otherwise. Fires on_shutdown at most once per idle stretch — a
        subsequent mark_activity rearms the watcher.
        """
        if self._triggered:
            return False
        if self.should_shutdown():
            self._triggered = True
            try:
                self._on_shutdown()
            except Exception:
                pass
            return True
        return False

    @property
    def triggered(self) -> bool:
        return self._triggered


@dataclass
class IdleWatcherStatus:
    seconds_idle: float
    seconds_remaining: float
    triggered: bool

    @classmethod
    def from_watcher(cls, w: IdleWatcher) -> "IdleWatcherStatus":
        idle = w.seconds_idle()
        return cls(
            seconds_idle=round(idle, 1),
            seconds_remaining=round(max(0.0, w.idle_timeout_s - idle), 1),
            triggered=w.triggered,
        )
