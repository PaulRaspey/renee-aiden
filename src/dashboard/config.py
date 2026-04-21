"""Dashboard config loader. Fail-closed on external binding."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@dataclass
class DashboardConfig:
    bind_host: str = "127.0.0.1"
    port: int = 7860
    password: str = ""
    state_dir: str = "state"
    config_dir: str = "configs"
    persona: str = "renee"
    mood_axis_max_delta: float = 0.2
    confirm_token: str = "confirm"
    sessions_root: Optional[str] = None

    @property
    def is_loopback(self) -> bool:
        host = (self.bind_host or "").strip().lower()
        if host in LOOPBACK_HOSTS:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @property
    def requires_password(self) -> bool:
        return not self.is_loopback

    def validate(self) -> None:
        """Raise if the config would bind externally without a password."""
        if self.requires_password and not (self.password or "").strip():
            raise DashboardConfigError(
                f"dashboard.bind_host={self.bind_host!r} is not a loopback "
                "address; a non-empty password is required in "
                "configs/dashboard.yaml before binding externally."
            )
        if not (1 <= int(self.port) <= 65535):
            raise DashboardConfigError(f"invalid dashboard.port={self.port}")

    @classmethod
    def load(cls, path: str | Path) -> "DashboardConfig":
        p = Path(path)
        if not p.exists():
            cfg = cls()
            cfg.validate()
            return cfg
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        sessions_root_raw = raw.get("sessions_root")
        cfg = cls(
            bind_host=str(raw.get("bind_host") or "127.0.0.1"),
            port=int(raw.get("port") or 7860),
            password=str(raw.get("password") or ""),
            state_dir=str(raw.get("state_dir") or "state"),
            config_dir=str(raw.get("config_dir") or "configs"),
            persona=str(raw.get("persona") or "renee"),
            mood_axis_max_delta=float(raw.get("mood_axis_max_delta") or 0.2),
            confirm_token=str(raw.get("confirm_token") or "confirm"),
            sessions_root=str(sessions_root_raw) if sessions_root_raw else None,
        )
        cfg.validate()
        return cfg


class DashboardConfigError(ValueError):
    """Raised when the dashboard config would be unsafe to serve."""
