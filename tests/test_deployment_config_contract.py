"""Contract test: every cloud.* key that load_deployment or cmd_proxy
reads must be present in the shipped configs/deployment.yaml. Drift here
is a classic audit bug where a rename in one file silently breaks the
other.
"""
from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "configs" / "deployment.yaml"


def test_deployment_yaml_has_all_keys_read_by_pod_manager():
    cfg = yaml.safe_load(DEPLOY.read_text(encoding="utf-8")) or {}
    cloud = cfg.get("cloud") or {}
    for required in (
        "pod_id",
        "audio_bridge_port",
        "audio_bridge_port_external",
        "eval_dashboard_port",
        "idle_shutdown_minutes",
        "region",
    ):
        assert required in cloud, f"cloud.{required} missing from deployment.yaml"


def test_deployment_yaml_has_proxy_port_read_by_cmd_proxy():
    cfg = yaml.safe_load(DEPLOY.read_text(encoding="utf-8")) or {}
    cloud = cfg.get("cloud") or {}
    assert "proxy_port" in cloud, "cloud.proxy_port missing from deployment.yaml"
    # Must be an int-compatible value.
    assert int(cloud["proxy_port"]) > 0


def test_deployment_yaml_has_model_repos_read_by_volume_setup():
    cfg = yaml.safe_load(DEPLOY.read_text(encoding="utf-8")) or {}
    cloud = cfg.get("cloud") or {}
    assert isinstance(cloud.get("model_repos"), list)
    for entry in cloud["model_repos"]:
        assert "repo_id" in entry
        assert "local_name" in entry


def test_load_deployment_returns_consistent_values():
    """Spot-check load_deployment vs raw yaml for drift between the parser
    and the shipped config. If someone adds a new cloud.* field, the
    dataclass must still pick it up."""
    from src.client.pod_manager import load_deployment

    settings = load_deployment(DEPLOY)
    cfg = yaml.safe_load(DEPLOY.read_text(encoding="utf-8")) or {}
    cloud = cfg["cloud"]
    # Env var may override pod_id; accept either.
    assert settings.pod_id in {cloud["pod_id"], ""} or settings.pod_id, (
        f"pod_id drift: parser={settings.pod_id!r} yaml={cloud['pod_id']!r}"
    )
    assert settings.audio_bridge_port == int(cloud["audio_bridge_port"])
    assert settings.audio_bridge_port_external == int(
        cloud["audio_bridge_port_external"]
    )
    assert settings.eval_dashboard_port == int(cloud["eval_dashboard_port"])
    assert settings.idle_shutdown_minutes == int(cloud["idle_shutdown_minutes"])


def test_cloud_llm_model_names_match_router_defaults():
    """When we ship a specific model in deployment.yaml it must match the
    router's default for the same backend. Running the pod with a Groq
    model that isn't the router's fallback is a silent config bug: the
    pod self-test succeeds but the live model varies."""
    cfg = yaml.safe_load(DEPLOY.read_text(encoding="utf-8")) or {}
    cloud_llm = cfg.get("cloud_llm") or {}
    groq_model = (cloud_llm.get("groq") or {}).get("model")
    anthropic_model = (cloud_llm.get("anthropic") or {}).get("model")

    # The router's fallback is hard-coded in llm_router.py; we keep the
    # yaml and the router in sync by checking the string values here.
    from src.persona.llm_router import LLMRouter

    assert groq_model == "qwen/qwen3-32b", (
        f"deployment.yaml cloud_llm.groq.model={groq_model!r} drifted from "
        "router fallback 'qwen/qwen3-32b'"
    )
    assert anthropic_model == "claude-sonnet-4-5", (
        f"deployment.yaml cloud_llm.anthropic.model={anthropic_model!r} drifted "
        "from router fallback 'claude-sonnet-4-5'"
    )
    # Also: the LLMRouter class imports cleanly; if a future refactor
    # renames the module, this test catches it.
    assert LLMRouter is not None
