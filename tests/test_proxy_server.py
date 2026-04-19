"""Unit tests for the mobile proxy server (src/client/proxy_server.py).

The real proxy uses the ``websockets`` library but all relay logic is
injectable so these tests exercise the full pump + reconnect flow with
an in-memory fake WebSocket and never bind a real port.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.client import proxy_server as ps


# -------------------------- fakes --------------------------------------


class FakeWS:
    """In-memory WebSocket stand-in: async-iterable of inbound frames
    with an ``outbox`` list collecting everything sent to it."""

    def __init__(self, messages=None):
        self._inbox: asyncio.Queue = asyncio.Queue()
        for m in messages or []:
            self._inbox.put_nowait(m)
        self.outbox: list = []
        self._closed = asyncio.Event()
        self._sentinel = object()
        self.close_code: int | None = None
        self.close_reason: str | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        get_task = asyncio.ensure_future(self._inbox.get())
        close_task = asyncio.ensure_future(self._closed.wait())
        done, pending = await asyncio.wait(
            {get_task, close_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except BaseException:
                pass
        if get_task in done:
            msg = get_task.result()
            if msg is self._sentinel:
                raise StopAsyncIteration
            return msg
        raise StopAsyncIteration

    async def send(self, data):
        if self._closed.is_set():
            raise ConnectionError("closed")
        self.outbox.append(data)

    async def close(self, code: int = 1000, reason: str = ""):
        if not self._closed.is_set():
            self.close_code = code
            self.close_reason = reason
            self._closed.set()
            await self._inbox.put(self._sentinel)

    def queue(self, msg) -> None:
        self._inbox.put_nowait(msg)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition never met: {predicate}")


# -------------------------- bridge URL ---------------------------------


def test_resolve_bridge_url_uses_static_bridge_host(tmp_path: Path):
    cfg = tmp_path / "deployment.yaml"
    cfg.write_text(
        "mode: cloud\n"
        "cloud:\n"
        "  pod_id: abc\n"
        "  bridge_host: 1.2.3.4\n"
        "  audio_bridge_port: 8765\n"
        "  audio_bridge_port_external: 10287\n",
        encoding="utf-8",
    )
    url = ps.resolve_bridge_url(cfg)
    assert url == "ws://1.2.3.4:10287"


def test_resolve_bridge_url_uses_internal_port_when_no_external(tmp_path: Path):
    cfg = tmp_path / "deployment.yaml"
    cfg.write_text(
        "mode: cloud\n"
        "cloud:\n"
        "  pod_id: abc\n"
        "  bridge_host: 1.2.3.4\n"
        "  audio_bridge_port: 8765\n",
        encoding="utf-8",
    )
    url = ps.resolve_bridge_url(cfg)
    assert url == "ws://1.2.3.4:8765"


def test_resolve_bridge_url_raises_when_unresolvable(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "deployment.yaml"
    cfg.write_text(
        "mode: cloud\n"
        "cloud:\n"
        "  pod_id: abc\n"
        "  audio_bridge_port: 8765\n",
        encoding="utf-8",
    )

    class FakeMgr:
        def __init__(self, *a, **kw): ...

        def status(self):
            return {"status": "STOPPED", "public_ip": ""}

    from src.client import pod_manager

    monkeypatch.setattr(pod_manager, "PodManager", FakeMgr)
    with pytest.raises(RuntimeError, match="no cloud.bridge_host"):
        ps.resolve_bridge_url(cfg)


# -------------------------- pump / reconnect --------------------------


@pytest.mark.asyncio
async def test_proxy_pipes_frames_bidirectionally():
    phone = FakeWS([b"mic_frame_1", b"mic_frame_2"])
    bridge = FakeWS(
        [b"tts_frame_1", '{"type":"transcript","speaker":"paul","text":"hi"}']
    )

    async def connect(_url):
        return bridge

    proxy = ps.RelayProxy("ws://fake", connect_bridge=connect, max_reconnects=0)
    task = asyncio.create_task(proxy.handle_phone(phone))
    await _wait_for(
        lambda: len(bridge.outbox) == 2 and len(phone.outbox) == 2,
        timeout=2.0,
    )
    await phone.close()
    await asyncio.wait_for(task, timeout=2.0)

    assert bridge.outbox == [b"mic_frame_1", b"mic_frame_2"]
    assert phone.outbox[0] == b"tts_frame_1"
    assert phone.outbox[1] == '{"type":"transcript","speaker":"paul","text":"hi"}'
    assert bridge.closed, "bridge must be closed after phone disconnects"


@pytest.mark.asyncio
async def test_proxy_reconnects_after_bridge_drop():
    phone = FakeWS()
    bridges = [FakeWS(), FakeWS()]
    attempts = 0

    async def connect(_url):
        nonlocal attempts
        b = bridges[attempts]
        attempts += 1
        return b

    proxy = ps.RelayProxy(
        "ws://fake",
        connect_bridge=connect,
        reconnect_delay_s=0.01,
        max_reconnects=3,
    )
    task = asyncio.create_task(proxy.handle_phone(phone))

    # First bridge is active; send one frame through it.
    await _wait_for(lambda: attempts == 1)
    phone.queue(b"frame_before_drop")
    await _wait_for(lambda: bridges[0].outbox == [b"frame_before_drop"])

    # Bridge drops — proxy should reconnect to bridges[1] without closing phone.
    await bridges[0].close()
    await _wait_for(lambda: attempts == 2, timeout=2.0)
    assert not phone.closed

    # Frame sent after reconnect lands on bridges[1].
    phone.queue(b"frame_after_reconnect")
    await _wait_for(lambda: bridges[1].outbox == [b"frame_after_reconnect"])

    await phone.close()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_proxy_closes_phone_after_max_bridge_failures():
    phone = FakeWS()

    async def connect(_url):
        raise OSError("bridge unreachable")

    proxy = ps.RelayProxy(
        "ws://fake",
        connect_bridge=connect,
        reconnect_delay_s=0.01,
        max_reconnects=2,
    )
    await asyncio.wait_for(proxy.handle_phone(phone), timeout=2.0)
    assert phone.closed
    assert phone.close_code == 1011


@pytest.mark.asyncio
async def test_proxy_handles_phone_disconnect_cleanly():
    phone = FakeWS()
    bridge = FakeWS()

    async def connect(_url):
        return bridge

    proxy = ps.RelayProxy("ws://fake", connect_bridge=connect, max_reconnects=0)
    task = asyncio.create_task(proxy.handle_phone(phone))
    await _wait_for(lambda: not bridge.closed)
    await phone.close()
    await asyncio.wait_for(task, timeout=2.0)
    assert bridge.closed


# -------------------------- static routes -----------------------------


def test_static_body_returns_none_for_unknown_route(tmp_path: Path):
    assert ps._static_body("/nope.png", tmp_path) is None


def test_static_body_returns_bytes_for_known_route(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html>hi</html>", encoding="utf-8")
    body, ctype = ps._static_body("/", tmp_path)
    assert body == b"<html>hi</html>"
    assert ctype.startswith("text/html")


def test_static_body_forces_js_content_type(tmp_path: Path):
    (tmp_path / "sw.js").write_text("self.addEventListener('install',()=>{});")
    _, ctype = ps._static_body("/sw.js", tmp_path)
    assert ctype == "application/javascript"


# -------------------------- tailscale detection -----------------------


def test_tailscale_ip_returns_first_ipv4():
    class Out:
        returncode = 0
        stdout = "100.64.0.5\nfd7a:115c:a1e0::1\n"

    def fake_run(*a, **kw):
        return Out()

    assert ps.tailscale_ip(runner=fake_run) == "100.64.0.5"


def test_tailscale_ip_returns_none_when_missing():
    def fake_run(*a, **kw):
        raise FileNotFoundError

    assert ps.tailscale_ip(runner=fake_run) is None


def test_tailscale_ip_returns_none_on_nonzero_exit():
    class Out:
        returncode = 1
        stdout = ""

    def fake_run(*a, **kw):
        return Out()

    assert ps.tailscale_ip(runner=fake_run) is None


def test_format_connect_urls_prefers_tailscale(monkeypatch):
    monkeypatch.setattr(ps, "tailscale_ip", lambda: "100.64.0.5")
    monkeypatch.setattr(ps, "local_ips", lambda: ["192.168.1.10"])
    urls = ps.format_connect_urls(8766)
    assert urls[0] == "http://100.64.0.5:8766/"
    assert "http://192.168.1.10:8766/" in urls


def test_format_connect_urls_falls_back_to_localhost(monkeypatch):
    monkeypatch.setattr(ps, "tailscale_ip", lambda: None)
    monkeypatch.setattr(ps, "local_ips", lambda: [])
    urls = ps.format_connect_urls(8766)
    assert urls == ["http://localhost:8766/"]
