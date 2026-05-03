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


# ---------------------------------------------------------------------------
# dashboard subcommand
# ---------------------------------------------------------------------------


def test_dashboard_subcommand_parses():
    parser = cli_main.build_parser()
    args = parser.parse_args(["dashboard", "--port", "7861", "--no-browser"])
    assert args.command == "dashboard"
    assert args.port == 7861
    assert args.no_browser is True


def test_dashboard_handler_skips_spawn_when_already_running(monkeypatch, capsys):
    """When /api/ping returns 200, the handler must not spawn another dashboard."""
    class _FakeCM:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: _FakeCM())
    spawned = []
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **kw: spawned.append((a, kw)) or SimpleNamespace(),
    )
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))

    args = SimpleNamespace(port=7860, no_browser=False)
    rc = cli_main.cmd_dashboard(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert spawned == []
    assert opened == ["http://127.0.0.1:7860"]
    assert "already running" in out


def test_dashboard_handler_spawns_when_down(monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("conn refused")))
    spawned = []
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **kw: spawned.append((a, kw)) or SimpleNamespace(),
    )
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("webbrowser.open", lambda u: None)

    args = SimpleNamespace(port=7860, no_browser=True)
    rc = cli_main.cmd_dashboard(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert len(spawned) == 1
    assert "starting" in out


# ---------------------------------------------------------------------------
# logs subcommand
# ---------------------------------------------------------------------------


def test_logs_subcommand_parses():
    parser = cli_main.build_parser()
    args = parser.parse_args(["logs", "--day", "2026-05-03", "-n", "10", "-f"])
    assert args.command == "logs"
    assert args.day == "2026-05-03"
    assert args.tail == 10
    assert args.follow is True


def test_logs_handler_returns_1_when_log_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_main, "REPO_ROOT", tmp_path)
    args = SimpleNamespace(day="2099-01-01", tail=50, follow=False)
    rc = cli_main.cmd_logs(args)
    assert rc == 1
    assert "no log" in capsys.readouterr().out.lower()


def test_logs_handler_prints_tail(tmp_path, monkeypatch, capsys):
    log_dir = tmp_path / "state" / "logs" / "conversations"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "2026-05-03.log"
    log_file.write_text(
        "\n".join(f"line-{i}" for i in range(20)),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_main, "REPO_ROOT", tmp_path)
    args = SimpleNamespace(day="2026-05-03", tail=5, follow=False)
    rc = cli_main.cmd_logs(args)
    out = capsys.readouterr().out
    assert rc == 0
    # Last 5 lines printed
    assert "line-19" in out
    assert "line-15" in out
    assert "line-14" not in out  # excluded by tail=5


# ---------------------------------------------------------------------------
# backup + preflight subcommands
# ---------------------------------------------------------------------------


def test_backup_subcommand_parses():
    parser = cli_main.build_parser()
    args = parser.parse_args(["backup", "--force"])
    assert args.command == "backup"
    assert args.force is True
    assert args.check is False


def test_preflight_subcommand_parses():
    parser = cli_main.build_parser()
    args = parser.parse_args(["preflight"])
    assert args.command == "preflight"


def test_preflight_handler_passes_when_all_green(monkeypatch, capsys):
    """Inject all four checks as passing; preflight should return 0."""
    # Patch the lazy-loaded launcher module after it's loaded inside cmd_preflight.
    # We do that by monkeypatching the module via sys.modules pre-emptively if
    # already loaded, or via the importlib.exec path otherwise. Simpler: patch
    # importlib.util.module_from_spec to return a stub.
    from types import SimpleNamespace as NS
    fake_mod = NS(
        _check_tailscale=lambda: (True, "100.x.x.x"),
        _check_pod=lambda: (True, {"id": "pod1", "public_ip": "1.2.3.4"}),
        _check_beacon=lambda: None,
        _check_daily_cap=lambda: None,
    )

    def fake_module_from_spec(spec):
        return fake_mod

    def fake_exec_module(mod):
        return None

    class FakeSpec:
        loader = SimpleNamespace(exec_module=fake_exec_module)

    monkeypatch.setattr("importlib.util.spec_from_file_location",
                        lambda *a, **kw: FakeSpec())
    monkeypatch.setattr("importlib.util.module_from_spec", fake_module_from_spec)

    rc = cli_main.cmd_preflight(SimpleNamespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "All checks passed" in out


def test_preflight_handler_fails_when_pod_down(monkeypatch, capsys):
    from types import SimpleNamespace as NS
    fake_mod = NS(
        _check_tailscale=lambda: (True, "100.x.x.x"),
        _check_pod=lambda: (False, {"status": "STOPPED"}),
        _check_beacon=lambda: None,
        _check_daily_cap=lambda: None,
    )

    class FakeSpec:
        loader = SimpleNamespace(exec_module=lambda m: None)

    monkeypatch.setattr("importlib.util.spec_from_file_location",
                        lambda *a, **kw: FakeSpec())
    monkeypatch.setattr("importlib.util.module_from_spec", lambda spec: fake_mod)

    rc = cli_main.cmd_preflight(SimpleNamespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT READY" in out
    assert "pod" in out


def test_preflight_handler_fails_when_cap_reached(monkeypatch, capsys):
    from types import SimpleNamespace as NS
    fake_mod = NS(
        _check_tailscale=lambda: (True, "100.x.x.x"),
        _check_pod=lambda: (True, {"id": "p", "public_ip": "1.2.3.4"}),
        _check_beacon=lambda: None,
        _check_daily_cap=lambda: {
            "used_minutes": 120.0, "cap_minutes": 120.0, "remaining_minutes": 0.0,
        },
    )

    class FakeSpec:
        loader = SimpleNamespace(exec_module=lambda m: None)

    monkeypatch.setattr("importlib.util.spec_from_file_location",
                        lambda *a, **kw: FakeSpec())
    monkeypatch.setattr("importlib.util.module_from_spec", lambda spec: fake_mod)

    rc = cli_main.cmd_preflight(SimpleNamespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert "CAP REACHED" in out
    assert "daily-cap-reached" in out
