"""Run the dashboard standalone.

    python -m src.dashboard

When the dashboard is embedded in the live pod, the cloud_startup script
builds it with an attached orchestrator + safety layer. This entry point
is the cold-read fallback PJ can use on the OptiPlex without wiring the
whole stack up.
"""
from __future__ import annotations

from pathlib import Path

import uvicorn

from .config import DashboardConfig
from .server import build_app


def main() -> None:
    cfg = DashboardConfig.load(Path("configs") / "dashboard.yaml")
    app = build_app(cfg)
    uvicorn.run(app, host=cfg.bind_host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
