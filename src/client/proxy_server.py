"""
OptiPlex-side WebSocket proxy for the mobile PWA client.

Exposes a single HTTP/WS endpoint on port 8766:

  - HTTP GET /               → PWA shell (index.html)
  - HTTP GET /manifest.json  → PWA manifest
  - HTTP GET /sw.js          → service worker
  - WS    /ws                → bidirectional audio + transcript relay

Each phone WebSocket opens a second WebSocket to the RunPod bridge and
pipes frames in both directions. Both binary (raw int16 PCM) and text
(JSON transcripts) are forwarded verbatim. If the bridge drops
mid-session the proxy reconnects transparently up to ``max_reconnects``
times before closing the phone side.

When the RTX Pro 6000 workstation is online this whole module collapses
into the in-process bridge — the HTTP + static routes are the same.

``websockets`` is imported lazily so the module imports cleanly on a
Python install that does not have it.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import socket
import ssl
import subprocess
from http import HTTPStatus
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


logger = logging.getLogger("renee.client.proxy_server")


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEPLOY_CONFIG = REPO_ROOT / "configs" / "deployment.yaml"
WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PROXY_PORT = 8766
WS_PATH = "/ws"


# --------------------------------------------------------------------------
# bridge URL resolution
# --------------------------------------------------------------------------


def resolve_bridge_url(deploy_config_path: str | Path = DEFAULT_DEPLOY_CONFIG) -> str:
    """Return ``ws://<host>:<port>`` for the RunPod bridge.

    Resolution order:
      1. ``cloud.bridge_host`` in deployment.yaml (static override).
      2. Live pod lookup via :class:`PodManager.status` (requires the
         runpod SDK and ``RUNPOD_API_KEY``).
    """
    import yaml

    from .pod_manager import load_deployment

    raw = yaml.safe_load(Path(deploy_config_path).read_text(encoding="utf-8")) or {}
    cloud = raw.get("cloud") or {}
    settings = load_deployment(deploy_config_path)
    static_host = str(cloud.get("bridge_host") or "").strip()
    if static_host:
        return settings.bridge_url_template.format(host=static_host)

    from .pod_manager import PodManager

    mgr = PodManager(settings)
    info = mgr.status()
    ip = info.get("public_ip") or ""
    if not ip:
        raise RuntimeError(
            "no cloud.bridge_host configured and pod has no public IP — "
            "run `python -m renee wake` first or set cloud.bridge_host in "
            "deployment.yaml"
        )
    return settings.bridge_url_template.format(host=ip)


# --------------------------------------------------------------------------
# relay core
# --------------------------------------------------------------------------


async def _default_connect_bridge(url: str):
    import websockets  # lazy

    return await websockets.connect(
        url,
        ping_interval=20,
        ping_timeout=20,
        max_size=None,
    )


class RelayProxy:
    """Bidirectional frame relay between a phone WS and the RunPod bridge."""

    def __init__(
        self,
        bridge_url: str | Callable[[], str],
        *,
        reconnect_delay_s: float = 2.0,
        max_reconnects: int = 3,
        connect_bridge: Optional[Callable[[str], Awaitable[Any]]] = None,
    ):
        self._bridge_url_arg = bridge_url
        self.reconnect_delay_s = reconnect_delay_s
        self.max_reconnects = max_reconnects
        self._connect_bridge = connect_bridge or _default_connect_bridge

    def bridge_url(self) -> str:
        if callable(self._bridge_url_arg):
            return self._bridge_url_arg()
        return self._bridge_url_arg

    async def handle_phone(self, phone_ws) -> None:
        """Serve one phone connection. Returns when the phone disconnects
        or the bridge is unreachable after ``max_reconnects`` attempts."""
        try:
            url = self.bridge_url()
        except Exception as e:
            logger.error("cannot resolve bridge URL: %s", e)
            await _safe_close(phone_ws, code=1011, reason="bridge URL unavailable")
            return

        logger.info("phone connected; target bridge %s", url)
        reconnects = 0
        while True:
            try:
                bridge_ws = await self._connect_bridge(url)
            except Exception as e:
                reconnects += 1
                if reconnects > self.max_reconnects:
                    logger.error(
                        "bridge unreachable after %d attempts: %s",
                        reconnects - 1,
                        e,
                    )
                    await _safe_close(phone_ws, code=1011, reason="bridge unavailable")
                    return
                logger.warning(
                    "bridge connect failed (attempt %d): %s; retrying in %.1fs",
                    reconnects,
                    e,
                    self.reconnect_delay_s,
                )
                await asyncio.sleep(self.reconnect_delay_s)
                continue

            reconnects = 0
            logger.info("bridge connected; piping frames")
            phone_closed_first = await _pump(phone_ws, bridge_ws)
            await _safe_close(bridge_ws)
            if phone_closed_first:
                logger.info("phone disconnected; shutting down relay")
                return
            logger.warning(
                "bridge dropped mid-session; reconnecting in %.1fs",
                self.reconnect_delay_s,
            )
            await asyncio.sleep(self.reconnect_delay_s)


async def _pump(phone_ws, bridge_ws) -> bool:
    """Pipe frames in both directions. Returns True when the phone side
    finished first (phone closed / phone→bridge pipe ended)."""
    p2b = asyncio.create_task(_pipe_frames(phone_ws, bridge_ws, "phone→bridge"))
    b2p = asyncio.create_task(_pipe_frames(bridge_ws, phone_ws, "bridge→phone"))
    done, pending = await asyncio.wait(
        {p2b, b2p}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    for t in pending:
        try:
            await t
        except BaseException:
            pass
    return p2b in done


async def _pipe_frames(src, dst, label: str) -> None:
    try:
        async for frame in src:
            try:
                await dst.send(frame)
            except Exception:
                logger.debug("send %s failed", label, exc_info=True)
                return
    except Exception:
        logger.debug("receive %s failed", label, exc_info=True)


async def _safe_close(ws, *, code: int = 1000, reason: str = "") -> None:
    close = getattr(ws, "close", None)
    if close is None:
        return
    try:
        result = close(code=code, reason=reason)
        if asyncio.iscoroutine(result):
            await result
    except TypeError:
        # Some fake/mocks don't accept kwargs.
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass
    except Exception:
        pass


# --------------------------------------------------------------------------
# static file server (via websockets.serve process_request hook)
# --------------------------------------------------------------------------


_STATIC_ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/manifest.json": "manifest.json",
    "/sw.js": "sw.js",
}


def _static_body(route: str, web_dir: Path = WEB_DIR) -> Optional[tuple[bytes, str]]:
    name = _STATIC_ROUTES.get(route)
    if name is None:
        return None
    path = web_dir / name
    if not path.is_file():
        return None
    ctype, _ = mimetypes.guess_type(path.name)
    if path.suffix == ".js":
        ctype = "application/javascript"
    return path.read_bytes(), ctype or "application/octet-stream"


def make_process_request(web_dir: Path = WEB_DIR):
    """Return an async ``process_request`` hook for ``websockets.serve``."""
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    async def process_request(connection, request):
        path = request.path.split("?", 1)[0]
        if path == WS_PATH:
            return None  # continue with WebSocket handshake
        static = _static_body(path, web_dir)
        if static is None:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        body, ctype = static
        headers = Headers(
            [
                ("Content-Type", ctype),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-cache"),
            ]
        )
        return Response(200, "OK", headers, body)

    return process_request


# --------------------------------------------------------------------------
# URL detection
# --------------------------------------------------------------------------


def tailscale_ip(runner: Optional[Callable[..., Any]] = None) -> Optional[str]:
    """Return the IPv4 reported by ``tailscale ip -4``, or ``None``.

    ``runner`` lets tests inject a subprocess-run fake.
    """
    run = runner or subprocess.run
    try:
        out = run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if getattr(out, "returncode", 1) != 0:
        return None
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line and ":" not in line:  # ignore IPv6
            return line
    return None


def local_ips() -> list[str]:
    """Best-effort list of non-loopback IPv4 addresses for this host."""
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip != "127.0.0.1" and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def format_connect_urls(port: int, scheme: str = "http") -> list[str]:
    urls: list[str] = []
    ts = tailscale_ip()
    if ts:
        urls.append(f"{scheme}://{ts}:{port}/")
    for ip in local_ips():
        url = f"{scheme}://{ip}:{port}/"
        if url not in urls:
            urls.append(url)
    if not urls:
        urls.append(f"{scheme}://localhost:{port}/")
    return urls


# --------------------------------------------------------------------------
# server main
# --------------------------------------------------------------------------


async def run_proxy(
    *,
    bridge_url: str | Callable[[], str],
    host: str = "0.0.0.0",
    port: int = DEFAULT_PROXY_PORT,
    web_dir: Path = WEB_DIR,
    ssl_context: Optional[ssl.SSLContext] = None,
    on_ready: Optional[Callable[[list[str]], None]] = None,
) -> None:
    """Start the proxy server; run until cancelled."""
    from websockets.asyncio.server import serve

    proxy = RelayProxy(bridge_url)
    process_request = make_process_request(web_dir)

    async def ws_handler(phone_ws):
        await proxy.handle_phone(phone_ws)

    scheme = "https" if ssl_context else "http"
    async with serve(
        ws_handler,
        host,
        port,
        process_request=process_request,
        ssl=ssl_context,
        max_size=None,
        ping_interval=20,
        ping_timeout=20,
    ):
        urls = format_connect_urls(port, scheme=scheme)
        logger.info("proxy listening on %s:%d", host, port)
        (on_ready or _print_connect_urls)(urls)
        await asyncio.Future()  # run until cancelled


def _print_connect_urls(urls: list[str]) -> None:
    primary = urls[0] if urls else ""
    if primary:
        print(f"Open on your phone: {primary}")
    for extra in urls[1:]:
        print(f"                 or: {extra}")
