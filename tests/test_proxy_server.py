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


def test_static_response_returns_none_for_unknown_route(tmp_path: Path):
    assert ps._static_response("/nope.png", tmp_path) is None


def test_static_response_returns_bytes_for_known_route(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html>hi</html>", encoding="utf-8")
    body, ctype, extra = ps._static_response("/", tmp_path)
    assert body == b"<html>hi</html>"
    assert ctype == "text/html; charset=utf-8"
    assert extra == {}


def test_static_response_sets_sw_allowed_header(tmp_path: Path):
    (tmp_path / "sw.js").write_text("self.addEventListener('install',()=>{});")
    body, ctype, extra = ps._static_response("/sw.js", tmp_path)
    assert ctype == "application/javascript"
    assert extra.get("Service-Worker-Allowed") == "/"


def test_static_response_manifest_mime_is_manifest_json(tmp_path: Path):
    (tmp_path / "manifest.json").write_text('{"name":"R"}', encoding="utf-8")
    _, ctype, _ = ps._static_response("/manifest.json", tmp_path)
    assert ctype == "application/manifest+json"


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


def test_tailscale_ip_matches_cli_when_available():
    """Live parity check. Only runs when a tailscale CLI is on PATH; the
    auto-detect must return exactly what the CLI reports, not a stale
    cache or a local/LAN address."""
    import shutil
    import subprocess

    if shutil.which("tailscale") is None:
        pytest.skip("tailscale CLI not on PATH")
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3, check=True,
        )
    except Exception as e:
        pytest.skip(f"tailscale ip -4 failed: {e}")
    cli_ip = next(
        (ln.strip() for ln in out.stdout.splitlines() if ln.strip()), None,
    )
    if not cli_ip:
        pytest.skip("tailscale reported no IPv4")
    assert ps.tailscale_ip() == cli_ip


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


# -------------------------- live server smoke -------------------------


@pytest.mark.asyncio
async def test_live_proxy_serves_cert_when_configured(tmp_path: Path, unused_tcp_port: int):
    """GET /cert returns the PEM body with an X.509 content type and a
    filename in Content-Disposition so iOS offers to install it."""
    import urllib.request

    cert_pem = (
        b"-----BEGIN CERTIFICATE-----\n"
        b"MIIFakeCertBody==\n"
        b"-----END CERTIFICATE-----\n"
    )
    cert_path = tmp_path / "renee.pem"
    cert_path.write_bytes(cert_pem)

    from websockets.asyncio.server import serve
    from src.client.proxy_server import make_process_request

    async def ws_handler(ws):
        await ws.wait_closed()

    process_request = make_process_request(tmp_path, cert_path=cert_path)
    async with serve(
        ws_handler, "127.0.0.1", unused_tcp_port, process_request=process_request
    ):
        def fetch():
            with urllib.request.urlopen(
                f"http://127.0.0.1:{unused_tcp_port}/cert", timeout=3
            ) as resp:
                return resp.status, resp.read(), dict(resp.headers.items())

        status, body, hdrs = await asyncio.to_thread(fetch)
        assert status == 200
        assert body == cert_pem
        assert hdrs["Content-Type"] == "application/x-x509-ca-cert"
        assert "attachment" in hdrs.get("Content-Disposition", "")


@pytest.mark.asyncio
async def test_live_proxy_cert_endpoint_404_when_unconfigured(
    tmp_path: Path, unused_tcp_port: int
):
    """With no cert_path configured, /cert must 404 (not leak any file)."""
    import urllib.request, urllib.error

    from websockets.asyncio.server import serve
    from src.client.proxy_server import make_process_request

    async def ws_handler(ws):
        await ws.wait_closed()

    async with serve(
        ws_handler, "127.0.0.1", unused_tcp_port,
        process_request=make_process_request(tmp_path, cert_path=None),
    ):
        def fetch():
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{unused_tcp_port}/cert", timeout=3
                ) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                return e.code

        assert await asyncio.to_thread(fetch) == 404


@pytest.mark.asyncio
async def test_live_server_serves_static_files(tmp_path: Path, unused_tcp_port: int):
    """End-to-end: bind a real port, fetch /, /manifest.json, /sw.js, and /missing."""
    import urllib.request
    import urllib.error

    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>renee</html>", encoding="utf-8")
    (web / "manifest.json").write_text('{"name":"R"}', encoding="utf-8")
    (web / "sw.js").write_text("/*sw*/", encoding="utf-8")

    from websockets.asyncio.server import serve

    async def ws_handler(ws):
        await ws.wait_closed()

    process_request = ps.make_process_request(web)
    async with serve(
        ws_handler,
        "127.0.0.1",
        unused_tcp_port,
        process_request=process_request,
    ):
        base = f"http://127.0.0.1:{unused_tcp_port}"

        def fetch(path: str) -> tuple[int, bytes, str, dict]:
            req = urllib.request.Request(base + path)
            try:
                with urllib.request.urlopen(req, timeout=3) as resp:
                    return (
                        resp.status,
                        resp.read(),
                        resp.headers.get("Content-Type", ""),
                        dict(resp.headers.items()),
                    )
            except urllib.error.HTTPError as e:
                return (
                    e.code,
                    e.read(),
                    e.headers.get("Content-Type", ""),
                    dict(e.headers.items()),
                )

        status, body, ctype, hdrs = await asyncio.to_thread(fetch, "/")
        assert status == 200
        assert body == b"<html>renee</html>"
        assert ctype == "text/html; charset=utf-8"

        status, body, ctype, _ = await asyncio.to_thread(fetch, "/manifest.json")
        assert status == 200
        assert body == b'{"name":"R"}'
        assert ctype == "application/manifest+json"

        status, body, ctype, hdrs = await asyncio.to_thread(fetch, "/sw.js")
        assert status == 200
        assert ctype == "application/javascript"
        assert hdrs.get("Service-Worker-Allowed") == "/"

        status, _, _, _ = await asyncio.to_thread(fetch, "/does-not-exist")
        assert status == 404


@pytest.fixture
def unused_tcp_port():
    import socket as _s

    s = _s.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# -------------------------- QR rendering -----------------------------


def test_render_qr_ascii_returns_nonempty_block_for_url():
    qr = ps.render_qr_ascii("http://100.64.0.5:8766/")
    assert qr, "qr renderer returned empty string with qrcode installed"
    lines = qr.strip().splitlines()
    # The square QR matrix must produce the same number of rows as columns
    # (each module renders as two characters, so row-chars == 2 * cols).
    assert len(lines) >= 15
    for ln in lines:
        assert len(ln) == 2 * len(lines)


def test_render_qr_ascii_is_plain_ascii():
    """Must be cp1252-safe so Windows consoles don't choke on output."""
    qr = ps.render_qr_ascii("http://x/")
    qr.encode("ascii")  # would raise UnicodeEncodeError on a half-block fallback


def test_render_qr_ascii_returns_empty_when_qrcode_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_qrcode(name, *a, **kw):
        if name == "qrcode":
            raise ImportError("simulated missing")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_qrcode)
    assert ps.render_qr_ascii("http://x/") == ""


def test_render_qr_png_writes_file_with_decodable_url(tmp_path: Path):
    """PNG fallback must be produced when the ``qrcode`` package is
    available, and it must decode back to the exact URL (scheme, host,
    port, trailing slash) so the phone doesn't land on a stripped link."""
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip("Pillow not installed; QR PNG decoding needs PIL")
    try:
        from pyzbar.pyzbar import decode as zbar_decode  # type: ignore
    except ImportError:
        # Fall back to reading the PNG bytes and at minimum asserting
        # the file exists and is a valid PNG. Full decode is optional.
        zbar_decode = None

    url = "https://100.64.0.5:8766/"
    out = ps.render_qr_png(url, tmp_path / "qr.png")
    assert out is not None, "render_qr_png returned None with qrcode installed"
    assert out.is_file()
    assert out.stat().st_size > 0
    header = out.read_bytes()[:8]
    assert header.startswith(b"\x89PNG\r\n\x1a\n")

    if zbar_decode is not None:
        img = Image.open(out)
        decoded = zbar_decode(img)
        assert decoded, "pyzbar could not read the QR image"
        payload = decoded[0].data.decode()
        assert payload == url
    else:
        # Re-encode the same URL via qrcode directly and byte-compare the
        # resulting PNG. This catches any URL mangling (stripped scheme,
        # port rewrite, trailing-slash drop) without needing a decoder.
        import qrcode
        from PIL import Image as _Image

        direct = qrcode.make(url, box_size=10, border=2)
        direct_path = tmp_path / "direct.png"
        direct.save(str(direct_path))
        assert _Image.open(out).tobytes() == _Image.open(direct_path).tobytes()


def test_render_qr_png_returns_none_when_qrcode_missing(tmp_path: Path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_qrcode(name, *a, **kw):
        if name == "qrcode":
            raise ImportError("simulated missing")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_qrcode)
    assert ps.render_qr_png("http://x/", tmp_path / "qr.png") is None


def test_terminal_supports_ascii_qr_reports_false_on_non_utf8_windows(
    monkeypatch,
):
    """The ASCII QR fallback must stay off on Windows CMD when the
    console code page isn't 65001 (UTF-8)."""
    monkeypatch.setattr(ps.sys, "platform", "win32")

    class FakeCtypes:
        class windll:
            class kernel32:
                @staticmethod
                def GetConsoleOutputCP():
                    return 437  # legacy OEM US code page

    monkeypatch.setattr("ctypes.windll", FakeCtypes.windll, raising=False)
    import ctypes as _c

    monkeypatch.setattr(_c, "windll", FakeCtypes.windll, raising=False)
    assert ps.terminal_supports_ascii_qr() is False


def test_terminal_supports_ascii_qr_returns_true_on_utf8_windows(monkeypatch):
    monkeypatch.setattr(ps.sys, "platform", "win32")

    class FakeCtypes:
        class windll:
            class kernel32:
                @staticmethod
                def GetConsoleOutputCP():
                    return 65001

    import ctypes as _c

    monkeypatch.setattr(_c, "windll", FakeCtypes.windll, raising=False)
    assert ps.terminal_supports_ascii_qr() is True


def test_terminal_supports_ascii_qr_on_non_windows(monkeypatch):
    monkeypatch.setattr(ps.sys, "platform", "linux")
    assert ps.terminal_supports_ascii_qr() is True


# -------------------------- live proxy + real bridge -----------------


async def _serve_websockets(handler, port: int):
    from websockets.asyncio.server import serve

    return await serve(handler, "127.0.0.1", port, max_size=None)


@pytest.mark.asyncio
async def test_live_proxy_pipes_real_websockets(unused_tcp_port: int):
    """End-to-end with real websockets on both sides of the proxy:
    phone -> proxy_ws -> bridge. Mic frames flow up; TTS flows down."""
    import websockets

    bridge_port = unused_tcp_port
    proxy_port = _grab_port()

    # Fake bridge: echo "{'ack':n}" text frames for every incoming PCM
    # frame, and push one transcript text frame on connect.
    async def bridge_handler(ws):
        await ws.send('{"type":"transcript","speaker":"paul","text":"hi"}')
        i = 0
        async for msg in ws:
            i += 1
            if isinstance(msg, (bytes, bytearray)):
                await ws.send(b"tts_" + str(i).encode())

    bridge = await _serve_websockets(bridge_handler, bridge_port)

    from src.client.proxy_server import run_proxy

    proxy_task = asyncio.create_task(
        run_proxy(
            bridge_url=f"ws://127.0.0.1:{bridge_port}",
            host="127.0.0.1",
            port=proxy_port,
            on_ready=lambda *_a, **_kw: None,
        )
    )
    try:
        await _wait_for_port(proxy_port)

        received_binary: list[bytes] = []
        received_text: list[str] = []
        async with websockets.connect(
            f"ws://127.0.0.1:{proxy_port}/ws"
        ) as phone:
            await phone.send(b"\x00" * 10)
            async for msg in phone:
                if isinstance(msg, (bytes, bytearray)):
                    received_binary.append(bytes(msg))
                    if len(received_binary) >= 1:
                        break
                else:
                    received_text.append(msg)
        assert received_binary == [b"tts_1"]
        assert any("paul" in t for t in received_text)
    finally:
        proxy_task.cancel()
        with pytest.raises((asyncio.CancelledError, BaseException)):
            await proxy_task
        bridge.close()
        await bridge.wait_closed()


@pytest.mark.asyncio
async def test_live_proxy_50x_connect_disconnect_no_leaks(unused_tcp_port: int):
    """Connect and disconnect 50 times; verify proxy state is empty and
    no bridge sockets linger after all clients are gone."""
    import websockets

    bridge_port = unused_tcp_port
    proxy_port = _grab_port()

    active_bridge_conns: set = set()

    async def bridge_handler(ws):
        active_bridge_conns.add(id(ws))
        try:
            async for _ in ws:
                pass
        finally:
            active_bridge_conns.discard(id(ws))

    bridge = await _serve_websockets(bridge_handler, bridge_port)

    # Inject a bridge connector that we can count; the proxy's internal
    # client registry is what we assert on afterward.
    from src.client.proxy_server import RelayProxy, make_process_request
    from websockets.asyncio.server import serve

    proxy = RelayProxy(f"ws://127.0.0.1:{bridge_port}")

    async def ws_handler(phone_ws):
        await proxy.handle_phone(phone_ws)

    server = await serve(
        ws_handler,
        "127.0.0.1",
        proxy_port,
        process_request=make_process_request(WEB := Path.cwd()),
        max_size=None,
    )
    try:
        await _wait_for_port(proxy_port)
        for _ in range(50):
            async with websockets.connect(
                f"ws://127.0.0.1:{proxy_port}/ws"
            ) as phone:
                await phone.send(b"\x00\x00\x00")
                # Small wait so the proxy has a chance to open the bridge
                # side; without this the client can drop the ws before
                # the proxy ever dials the bridge.
                await asyncio.sleep(0.01)
            # Give the proxy handler a few ticks to unwind cleanly.
            await asyncio.sleep(0.01)
        # Allow final bridge closes to propagate.
        for _ in range(50):
            if proxy.active_client_count == 0 and not active_bridge_conns:
                break
            await asyncio.sleep(0.02)
        assert proxy.active_client_count == 0, (
            f"proxy leaked {proxy.active_client_count} clients"
        )
        assert not active_bridge_conns, (
            f"bridge leaked {len(active_bridge_conns)} sockets"
        )
    finally:
        server.close()
        await server.wait_closed()
        bridge.close()
        await bridge.wait_closed()


@pytest.mark.asyncio
async def test_live_proxy_reconnects_across_real_bridge_drop():
    """Bring up bridge A; connect a browser; drop the bridge; bring up a
    replacement on a different port; assert the phone socket stays open
    and traffic flows across the break.

    Using two distinct bridge ports avoids the port-rebind race that can
    hang on Windows (TIME_WAIT + a retry loop can hammer the socket).
    The proxy's bridge_url is a callable that picks the currently-live
    bridge, which is exactly how a real resolver would behave if the
    RunPod IP changed.
    """
    import websockets

    port_a = _grab_port()
    port_b = _grab_port()
    proxy_port = _grab_port()

    async def make_bridge(port: int, label: str):
        async def handler(ws):
            await ws.send(f"hello-from-{label}".encode())
            try:
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        await ws.send(f"ack-{label}".encode())
            except Exception:
                pass
        return await _serve_websockets(handler, port)

    bridge_a = await make_bridge(port_a, "A")
    current_port = [port_a]

    def bridge_url():
        return f"ws://127.0.0.1:{current_port[0]}"

    from src.client.proxy_server import RelayProxy
    from websockets.asyncio.server import serve

    proxy = RelayProxy(bridge_url, reconnect_delay_s=0.05, max_reconnects=200)

    async def ws_handler(phone_ws):
        await proxy.handle_phone(phone_ws)

    server = await serve(ws_handler, "127.0.0.1", proxy_port, max_size=None)
    bridge_b = None
    try:
        await _wait_for_port(proxy_port)
        async with websockets.connect(
            f"ws://127.0.0.1:{proxy_port}/ws", open_timeout=3
        ) as phone:
            greeting = await asyncio.wait_for(phone.recv(), timeout=2)
            assert greeting == b"hello-from-A"
            await phone.send(b"x")
            assert await asyncio.wait_for(phone.recv(), timeout=2) == b"ack-A"

            # Swap to bridge B before dropping A, so the resolver returns
            # B on the proxy's next reconnect. We do not await
            # bridge_a.wait_closed() here because websockets' Server
            # waits for handler tasks to finish, and the proxy's handler
            # is in the middle of reconnecting, causing a deadlock on
            # Windows. close(close_connections=True) is enough to force
            # the proxy's bridge_ws to drop.
            bridge_b = await make_bridge(port_b, "B")
            current_port[0] = port_b
            bridge_a.close(close_connections=True)

            # Phone must stay open while the proxy reconnects to B.
            greeting_b = await asyncio.wait_for(phone.recv(), timeout=5)
            assert greeting_b == b"hello-from-B"
            await phone.send(b"y")
            assert await asyncio.wait_for(phone.recv(), timeout=3) == b"ack-B"
    finally:
        server.close()
        await server.wait_closed()
        if bridge_b is not None:
            bridge_b.close()
            await bridge_b.wait_closed()


@pytest.mark.asyncio
async def test_two_phones_each_receive_only_their_bridge_transcripts():
    """Concurrent phone clients must not see each other's transcripts.
    Phone A's proxy connection opens bridge A which emits transcripts
    tagged "A"; phone B likewise gets "B". Each phone must observe only
    its own tag."""
    import websockets

    port_a = _grab_port()
    port_b = _grab_port()
    proxy_port = _grab_port()

    async def bridge_handler(ws, label: str):
        # Send a distinct text frame per connection.
        await ws.send(
            f'{{"type":"transcript","speaker":"paul","text":"hello-{label}"}}'
        )
        try:
            async for _ in ws:
                pass
        except Exception:
            pass

    from websockets.asyncio.server import serve
    bridge_a = await serve(lambda ws: bridge_handler(ws, "A"),
                           "127.0.0.1", port_a, max_size=None)
    bridge_b = await serve(lambda ws: bridge_handler(ws, "B"),
                           "127.0.0.1", port_b, max_size=None)

    from src.client.proxy_server import RelayProxy

    # Each phone gets its OWN proxy instance with its OWN bridge URL, so
    # this test proves the per-client isolation is structural (no shared
    # mutable state leaks one phone's transcripts to another).
    proxy_a = RelayProxy(f"ws://127.0.0.1:{port_a}",
                         reconnect_delay_s=0.05, max_reconnects=5)
    proxy_b = RelayProxy(f"ws://127.0.0.1:{port_b}",
                         reconnect_delay_s=0.05, max_reconnects=5)

    # Both phone clients land on the same proxy server port; the server
    # dispatches based on connection id, not a shared orchestrator.
    clients_seen: list = []

    async def ws_handler(phone_ws):
        # Alternate which proxy instance handles each accept, so at least
        # one phone goes to each. In production the single proxy handles
        # all phones; this structure is just easier to reason about for
        # the isolation assertion.
        idx = len(clients_seen)
        clients_seen.append(idx)
        proxy = proxy_a if idx == 0 else proxy_b
        await proxy.handle_phone(phone_ws)

    server = await serve(
        ws_handler, "127.0.0.1", proxy_port, max_size=None
    )
    try:
        await _wait_for_port(proxy_port)
        a = await websockets.connect(
            f"ws://127.0.0.1:{proxy_port}/ws", open_timeout=3
        )
        b = await websockets.connect(
            f"ws://127.0.0.1:{proxy_port}/ws", open_timeout=3
        )
        try:
            msg_a = await asyncio.wait_for(a.recv(), timeout=3)
            msg_b = await asyncio.wait_for(b.recv(), timeout=3)
            assert "hello-A" in msg_a and "hello-B" not in msg_a
            assert "hello-B" in msg_b and "hello-A" not in msg_b
        finally:
            await a.close()
            await b.close()
    finally:
        server.close()
        await server.wait_closed()
        bridge_a.close(close_connections=True)
        bridge_b.close(close_connections=True)


@pytest.mark.asyncio
async def test_live_proxy_closes_phone_with_1011_when_bridge_absent():
    """Bridge never comes up; after max_reconnects the proxy closes the
    phone socket with code 1011 (server error) and a reason the client
    can log."""
    import websockets

    proxy_port = _grab_port()
    no_such_bridge_port = _grab_port()

    from src.client.proxy_server import RelayProxy
    from websockets.asyncio.server import serve

    # On Windows, websockets.connect to a refused port takes ~2s before
    # raising ConnectionRefusedError (asyncio proactor quirk), so even a
    # trivial give-up test spans several seconds. max_reconnects=0 means
    # "one attempt, fail, close" which keeps the test under ~3s on win32.
    proxy = RelayProxy(
        f"ws://127.0.0.1:{no_such_bridge_port}",
        reconnect_delay_s=0.02,
        max_reconnects=0,
    )

    async def ws_handler(phone_ws):
        await proxy.handle_phone(phone_ws)

    server = await serve(ws_handler, "127.0.0.1", proxy_port, max_size=None)
    try:
        await _wait_for_port(proxy_port)
        phone = await websockets.connect(
            f"ws://127.0.0.1:{proxy_port}/ws", open_timeout=3
        )
        try:
            with pytest.raises(websockets.ConnectionClosed) as excinfo:
                await asyncio.wait_for(phone.recv(), timeout=8)
            assert excinfo.value.code == 1011
            assert "bridge unavailable" in (excinfo.value.reason or "")
        finally:
            await phone.close()
    finally:
        server.close()
        await server.wait_closed()


def _grab_port() -> int:
    import socket as _s

    s = _s.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _wait_for_port(port: int, timeout: float = 3.0) -> None:
    import socket as _s

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        with _s.socket() as s:
            try:
                s.settimeout(0.25)
                s.connect(("127.0.0.1", port))
                return
            except OSError:
                await asyncio.sleep(0.02)
    raise TimeoutError(f"port {port} never opened")


# ---------------------------------------------------------------------------
# Phone-side status helpers (#9)
# ---------------------------------------------------------------------------


def test_status_routes_present_in_static_table():
    """The new /status endpoints must be in _STATIC_ROUTES so the proxy
    serves them. Catches accidental removal."""
    assert "/status" in ps._STATIC_ROUTES
    assert "/status.html" in ps._STATIC_ROUTES
    assert "/status.js" in ps._STATIC_ROUTES
    # Content types match what mobile browsers expect
    assert ps._STATIC_ROUTES["/status"][1] == "text/html; charset=utf-8"
    assert ps._STATIC_ROUTES["/status.js"][1] == "application/javascript"


def test_phone_status_snapshot_with_mocked_pod(monkeypatch):
    """The snapshot collects pod + cost + beacon. Mock all three so we can
    exercise the assembly logic without hitting RunPod / Beacon / disk."""
    fake_pod = {
        "id": "p1", "status": "RUNNING", "public_ip": "1.2.3.4",
        "uptime_seconds": 1800, "gpu_type": "NVIDIA A100 SXM",
    }
    monkeypatch.setattr(
        "src.client.pod_manager.PodManager.status",
        lambda self: fake_pod,
    )
    monkeypatch.delenv("BEACON_URL", raising=False)
    snap = ps._phone_status_snapshot()
    assert snap["pod"]["status"] == "RUNNING"
    assert snap["pod"]["gpu_type"] == "NVIDIA A100 SXM"
    # 30 min × $1.50/hr = $0.75
    assert snap["cost"]["session_usd"] == pytest.approx(0.75)
    assert snap["cost"]["hourly_usd"] == 1.50
    # No BEACON_URL -> not configured
    assert snap["beacon"]["configured"] is False


def test_phone_status_snapshot_handles_pod_unreachable(monkeypatch):
    def boom(self):
        raise RuntimeError("no API key")
    monkeypatch.setattr("src.client.pod_manager.PodManager.status", boom)
    monkeypatch.delenv("BEACON_URL", raising=False)
    snap = ps._phone_status_snapshot()
    assert snap["pod"]["ok"] is False
    assert "error" in snap["pod"]
    # Cost still computes (uptime=0 -> $0)
    assert snap["cost"]["session_usd"] == 0.0


def test_phone_status_snapshot_beacon_unreachable_records_state(monkeypatch):
    monkeypatch.setattr(
        "src.client.pod_manager.PodManager.status",
        lambda self: {
            "id": "p", "status": "RUNNING", "public_ip": "x",
            "uptime_seconds": 0, "gpu_type": "L40S",
        },
    )
    monkeypatch.setenv("BEACON_URL", "http://127.0.0.1:1")  # nothing listens here
    snap = ps._phone_status_snapshot()
    assert snap["beacon"]["configured"] is True
    assert snap["beacon"]["reachable"] is False


def test_phone_sleep_now_returns_ok_on_success(monkeypatch):
    monkeypatch.setattr(
        "src.client.pod_manager.PodManager.sleep",
        lambda self: {"status": "STOPPED", "pod_id": "p"},
    )
    result = ps._phone_sleep_now()
    assert result["ok"] is True
    assert result["info"]["status"] == "STOPPED"


def test_phone_sleep_now_records_failure(monkeypatch):
    def boom(self):
        raise RuntimeError("auth failed")
    monkeypatch.setattr("src.client.pod_manager.PodManager.sleep", boom)
    result = ps._phone_sleep_now()
    assert result["ok"] is False
    assert "auth failed" in result["error"]
