"""
OptiPlex-side WebSocket proxy for the mobile PWA client.

Exposes a single HTTP/WS endpoint on port 8766:

  HTTP GET /               PWA shell (index.html)
  HTTP GET /manifest.json  PWA manifest (application/manifest+json)
  HTTP GET /sw.js          service worker (Service-Worker-Allowed: /)
  HTTP GET /cert           self-signed CA cert download, for iOS trust
  WS    /ws                bidirectional audio + transcript relay

Each phone WebSocket opens a second WebSocket to the RunPod bridge and
pipes frames in both directions. Both binary (raw int16 PCM) and text
(JSON transcripts) are forwarded verbatim. If the bridge drops
mid-session the proxy reconnects transparently up to ``max_reconnects``
times before closing the phone side with code 1011.

When the RTX Pro 6000 workstation is online this whole module collapses
into the in-process bridge; the HTTP and static routes are the same.

``websockets`` is imported lazily so the module imports cleanly on a
Python install that does not have it.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import socket
import ssl
import subprocess
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


logger = logging.getLogger("renee.client.proxy_server")


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEPLOY_CONFIG = REPO_ROOT / "configs" / "deployment.yaml"
WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PROXY_PORT = 8766
WS_PATH = "/ws"
CERT_PATH = "/cert"


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
            "no cloud.bridge_host configured and pod has no public IP. "
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
        # Active per-client state for diagnostics and leak detection. Keys
        # are connection ids allocated on accept; entries auto-remove on
        # handler exit.
        self._clients: dict[int, dict] = {}
        self._next_id: int = 0

    @property
    def active_client_count(self) -> int:
        return len(self._clients)

    def bridge_url(self) -> str:
        if callable(self._bridge_url_arg):
            return self._bridge_url_arg()
        return self._bridge_url_arg

    async def handle_phone(self, phone_ws) -> None:
        """Serve one phone connection. Returns when the phone disconnects
        or the bridge is unreachable after ``max_reconnects`` attempts."""
        cid = self._next_id
        self._next_id += 1
        self._clients[cid] = {"phone_ws": phone_ws, "bridge_ws": None}
        try:
            await self._serve(cid, phone_ws)
        finally:
            self._clients.pop(cid, None)

    async def _serve(self, cid: int, phone_ws) -> None:
        reconnects = 0
        logger.info("cid=%d phone connected", cid)
        while True:
            # Resolve the URL every iteration. When bridge_url is a
            # callable (e.g. resolve_bridge_url backed by pod_manager),
            # the pod's public IP can change mid-session; we want the
            # reconnect to pick up the new value rather than hammer a
            # dead address.
            try:
                url = self.bridge_url()
            except Exception as e:
                logger.error("cid=%d cannot resolve bridge URL: %s", cid, e)
                await _safe_close(
                    phone_ws, code=1011, reason="bridge URL unavailable"
                )
                return

            try:
                bridge_ws = await self._connect_bridge(url)
            except Exception as e:
                reconnects += 1
                if reconnects > self.max_reconnects:
                    logger.error(
                        "cid=%d bridge unreachable after %d attempts: %s",
                        cid,
                        reconnects - 1,
                        e,
                    )
                    await _safe_close(
                        phone_ws, code=1011, reason="bridge unavailable"
                    )
                    return
                delay = min(self.reconnect_delay_s * (2 ** (reconnects - 1)), 30.0)
                logger.warning(
                    "cid=%d bridge connect failed (attempt %d): %s; retry in %.1fs",
                    cid,
                    reconnects,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            self._clients[cid]["bridge_ws"] = bridge_ws
            reconnects = 0
            logger.info("cid=%d bridge connected; piping frames", cid)
            try:
                phone_closed_first = await _pump(phone_ws, bridge_ws)
            finally:
                await _safe_close(bridge_ws)
                self._clients[cid]["bridge_ws"] = None
            if phone_closed_first:
                logger.info("cid=%d phone disconnected; shutting down relay", cid)
                return
            delay = self.reconnect_delay_s
            logger.warning(
                "cid=%d bridge dropped mid-session; reconnect in %.1fs", cid, delay,
            )
            await asyncio.sleep(delay)


async def _pump(phone_ws, bridge_ws) -> bool:
    """Pipe frames in both directions. Returns True when the phone side
    finished first (phone closed or phone->bridge pipe ended)."""
    p2b = asyncio.create_task(_pipe_frames(phone_ws, bridge_ws, "phone->bridge"))
    b2p = asyncio.create_task(_pipe_frames(bridge_ws, phone_ws, "bridge->phone"))
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


# Explicit MIME map: we intentionally do not consult mimetypes.guess_type
# for these routes because PWA correctness depends on the exact values.
# Safari/Chrome treat application/json differently from
# application/manifest+json; Chrome rejects a service worker if the MIME
# isn't one of the JS MIME types, and iOS Safari warns loudly if the cert
# download isn't served with an X.509 content type.
_STATIC_ROUTES: dict[str, tuple[str, str, dict[str, str]]] = {
    # path            : (filename,        content-type,                        extra-headers)
    "/":              ("index.html",     "text/html; charset=utf-8",          {}),
    "/index.html":    ("index.html",     "text/html; charset=utf-8",          {}),
    "/manifest.json": ("manifest.json",  "application/manifest+json",         {}),
    "/client.js":     ("client.js",      "application/javascript",            {}),
    "/sw.js":         ("sw.js",          "application/javascript",
                       {"Service-Worker-Allowed": "/"}),
}


def _static_response(
    route: str, web_dir: Path = WEB_DIR
) -> Optional[tuple[bytes, str, dict[str, str]]]:
    entry = _STATIC_ROUTES.get(route)
    if entry is None:
        return None
    name, ctype, extra = entry
    path = web_dir / name
    if not path.is_file():
        return None
    return path.read_bytes(), ctype, extra


def make_process_request(
    web_dir: Path = WEB_DIR,
    *,
    cert_path: Optional[Path] = None,
):
    """Return an async ``process_request`` hook for ``websockets.serve``.

    ``cert_path`` enables GET /cert, serving the PEM cert as a downloadable
    .crt with an application/x-x509-ca-cert content type so iOS Safari
    offers to install it. Pass None to disable the route (404 on /cert).
    """
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    def _build(status: int, reason: str, body: bytes, ctype: str,
               extra: Optional[dict[str, str]] = None) -> Response:
        headers_list = [
            ("Content-Type", ctype),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-cache"),
        ]
        for k, v in (extra or {}).items():
            headers_list.append((k, v))
        return Response(status, reason, Headers(headers_list), body)

    async def process_request(connection, request):
        path = request.path.split("?", 1)[0]
        if path == WS_PATH:
            return None  # continue with WebSocket handshake

        if path == CERT_PATH:
            if cert_path and Path(cert_path).is_file():
                body = Path(cert_path).read_bytes()
                return _build(
                    200, "OK", body, "application/x-x509-ca-cert",
                    {"Content-Disposition": 'attachment; filename="renee-proxy.crt"'},
                )
            return _build(404, "Not Found", b"no cert configured\n",
                          "text/plain; charset=utf-8")

        static = _static_response(path, web_dir)
        if static is None:
            return _build(404, "Not Found", b"not found\n",
                          "text/plain; charset=utf-8")
        body, ctype, extra = static
        return _build(200, "OK", body, ctype, extra)

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
# QR code (terminal ASCII + PNG fallback for non-UTF-8 code pages)
# --------------------------------------------------------------------------


def terminal_supports_ascii_qr() -> bool:
    """Return True when the current stdout can render ``##``-based QRs cleanly.

    The ASCII renderer is pure 7-bit so technically any code page works,
    but on Windows CMD with legacy ``cp437`` or ``cp850`` the terminal font
    often uses non-square glyphs that make the QR unscannable. When we
    detect a non-UTF-8 Windows console we recommend the PNG fallback.
    """
    if sys.platform != "win32":
        return True
    # On Windows, the console code page matters more than sys.stdout.encoding.
    try:
        import ctypes  # type: ignore

        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    except Exception:
        cp = 0
    if cp == 65001:  # UTF-8 code page
        return True
    # Anything else: fall back to PNG to guarantee scannability.
    return False


def render_qr_ascii(url: str) -> str:
    """Return a QR code rendered with plain 7-bit ASCII (``##`` dark,
    two spaces light). Empty string if the ``qrcode`` package is missing."""
    try:
        import qrcode  # lazy
    except ImportError:
        return ""

    qr = qrcode.QRCode(border=2, box_size=1)
    qr.add_data(url)
    qr.make(fit=True)
    lines: list[str] = []
    for row in qr.modules:
        lines.append("".join("##" if m else "  " for m in row))
    return "\n".join(lines) + "\n"


def render_qr_png(url: str, out_path: Path) -> Optional[Path]:
    """Write a PNG QR of ``url`` to ``out_path`` and return the path.

    Returns None if neither ``qrcode`` nor PIL is available. The caller
    should treat None as "printed the URL only" and move on.
    """
    try:
        import qrcode  # lazy
    except ImportError:
        return None
    try:
        img = qrcode.make(url, box_size=10, border=2)
    except Exception:  # PIL missing or QR failed
        return None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path))
    except Exception:
        return None
    return out_path


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
    cert_path: Optional[Path] = None,
    on_ready: Optional[Callable[[list[str], Optional[Path]], None]] = None,
    qr_png_path: Optional[Path] = None,
) -> None:
    """Start the proxy server; run until cancelled."""
    from websockets.asyncio.server import serve

    proxy = RelayProxy(bridge_url)
    process_request = make_process_request(web_dir, cert_path=cert_path)

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
        png = None
        if qr_png_path is not None and urls:
            png = render_qr_png(urls[0], qr_png_path)
        (on_ready or _print_connect_urls)(urls, png)
        await asyncio.Future()  # run until cancelled


def _print_connect_urls(urls: list[str], png: Optional[Path]) -> None:
    primary = urls[0] if urls else ""
    if primary:
        print(f"Open on your phone: {primary}")
        if primary.startswith("https://"):
            print(
                "First time on this device: tap the URL, accept the cert, "
                f"then install the CA from {primary.rstrip('/')}/cert."
            )
        if terminal_supports_ascii_qr():
            qr_ascii = render_qr_ascii(primary)
            if qr_ascii:
                print(qr_ascii)
        else:
            print(
                "(terminal code page is not UTF-8; ASCII QR would be "
                "unscannable)"
            )
        if png is not None:
            print(f"QR image saved to: {os.path.abspath(png)}")
    for extra in urls[1:]:
        print(f"                 or: {extra}")
