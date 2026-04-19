"""Tests for src.cli.main CLI dispatcher."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.cli import main as cli_main


def test_build_parser_accepts_all_commands():
    parser = cli_main.build_parser()
    # Just a smoke test — each known command parses without error.
    for cmd in ("wake", "talk", "sleep", "status", "text", "eval", "proxy"):
        args = parser.parse_args([cmd])
        assert args.command == cmd


def test_proxy_subcommand_parses_flags():
    parser = cli_main.build_parser()
    args = parser.parse_args(
        ["proxy", "--port", "9000", "--bridge-url", "ws://x:1",
         "--no-browser", "--https"]
    )
    assert args.command == "proxy"
    assert args.port == 9000
    assert args.bridge_url == "ws://x:1"
    assert args.no_browser is True
    assert args.https is True


def test_proxy_handler_bails_when_bridge_url_unresolvable(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "deployment.yaml"
    cfg.write_text(
        "mode: cloud\ncloud:\n  pod_id: x\n  audio_bridge_port: 8765\n",
        encoding="utf-8",
    )

    def boom(_p):
        raise RuntimeError("pod has no public IP")

    monkeypatch.setattr("src.client.proxy_server.resolve_bridge_url", boom)

    args = SimpleNamespace(
        deploy_config=str(cfg),
        port=None,
        bridge_url=None,
        no_browser=True,
        https=False,
    )
    rc = cli_main.cmd_proxy(args)
    err = capsys.readouterr().err
    assert rc == 2
    assert "bridge url" in err.lower()


def test_proxy_handler_respects_explicit_bridge_url(monkeypatch, tmp_path):
    cfg = tmp_path / "deployment.yaml"
    cfg.write_text(
        "mode: cloud\ncloud:\n  pod_id: x\n  audio_bridge_port: 8765\n  proxy_port: 9999\n",
        encoding="utf-8",
    )

    captured = {}

    async def fake_run_proxy(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("src.client.proxy_server.run_proxy", fake_run_proxy)

    args = SimpleNamespace(
        deploy_config=str(cfg),
        port=None,
        bridge_url="ws://explicit:1",
        no_browser=True,
        https=False,
    )
    rc = cli_main.cmd_proxy(args)
    assert rc == 0
    assert captured["bridge_url"] == "ws://explicit:1"
    assert captured["port"] == 9999
    assert captured["ssl_context"] is None
    # cert_path is None when --https is off.
    assert captured.get("cert_path") is None


def test_export_command_accepts_output_flag():
    parser = cli_main.build_parser()
    args = parser.parse_args(["export", "--output", "/tmp/out"])
    assert args.command == "export"
    assert args.output == "/tmp/out"


def test_main_prints_help_without_command(capsys):
    exit_code = cli_main.main([])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Talk to Renée" in out or "usage:" in out.lower()


def test_export_handler_copies_state(tmp_path, monkeypatch, capsys):
    # Point REPO_ROOT at a fake state dir with one file to copy.
    fake_repo = tmp_path / "repo"
    (fake_repo / "state").mkdir(parents=True)
    (fake_repo / "state" / "foo.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(cli_main, "REPO_ROOT", fake_repo)

    args = SimpleNamespace(output=str(tmp_path / "out"))
    rc = cli_main.cmd_export(args)
    assert rc == 0
    out_file = tmp_path / "out" / "foo.txt"
    assert out_file.exists()
    assert out_file.read_text(encoding="utf-8") == "hello"


def test_wake_handler_invokes_pod_manager(monkeypatch, capsys):
    class FakeMgr:
        def __init__(self, *a, **k):
            pass

        def wake(self, **kwargs):
            return {"status": "RUNNING", "public_ip": "1.1.1.1", "bridge_url": "ws://1.1.1.1:8765"}

    monkeypatch.setattr("src.client.pod_manager.PodManager", FakeMgr)
    monkeypatch.setattr(
        "src.client.pod_manager.load_deployment",
        lambda _p: SimpleNamespace(
            pod_id="x", region="US-TX",
            audio_bridge_port=8765, eval_dashboard_port=7860,
            idle_shutdown_minutes=60, mode="cloud",
            bridge_url_template="ws://{host}:8765",
        ),
    )
    args = SimpleNamespace(deploy_config="ignored")
    rc = cli_main.cmd_wake(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "RUNNING" in out


def test_sleep_handler_invokes_pod_manager(monkeypatch, capsys):
    class FakeMgr:
        def __init__(self, *a, **k):
            pass

        def sleep(self):
            return {"status": "STOPPED", "pod_id": "x"}

    monkeypatch.setattr("src.client.pod_manager.PodManager", FakeMgr)
    monkeypatch.setattr(
        "src.client.pod_manager.load_deployment",
        lambda _p: SimpleNamespace(
            pod_id="x", region="US-TX",
            audio_bridge_port=8765, eval_dashboard_port=7860,
            idle_shutdown_minutes=60, mode="cloud",
            bridge_url_template="ws://{host}:8765",
        ),
    )
    args = SimpleNamespace(deploy_config="ignored")
    rc = cli_main.cmd_sleep(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "STOPPED" in out


def test_talk_handler_bails_when_pod_not_running(monkeypatch, capsys):
    class FakeMgr:
        def __init__(self, *a, **k):
            pass

        def status(self):
            return {"status": "STOPPED", "public_ip": ""}

    monkeypatch.setattr("src.client.pod_manager.PodManager", FakeMgr)
    monkeypatch.setattr(
        "src.client.pod_manager.load_deployment",
        lambda _p: SimpleNamespace(
            pod_id="x", region="US-TX",
            audio_bridge_port=8765, eval_dashboard_port=7860,
            idle_shutdown_minutes=60, mode="cloud",
            bridge_url_template="ws://{host}:8765",
        ),
    )
    args = SimpleNamespace(deploy_config="ignored")
    rc = cli_main.cmd_talk(args)
    out = capsys.readouterr().out
    assert rc == 2
    assert "not running" in out.lower()
