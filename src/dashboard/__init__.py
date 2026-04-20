"""M15 observability and tuning console.

Single-page web app backed by FastAPI. Five tabs: Live, Tuning, Logs,
Health, Eval. Fails closed on external binding without a password.

Entry point:
    from src.dashboard import build_app, DashboardConfig
    cfg = DashboardConfig.load(Path("configs/dashboard.yaml"))
    app = build_app(cfg, orchestrator=orchestrator, safety_layer=safety)

Running standalone:
    python -m src.dashboard
"""
from .config import DashboardConfig
from .server import build_app

__all__ = ["DashboardConfig", "build_app"]
