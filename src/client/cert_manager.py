"""
Self-signed certificate generator for the mobile proxy.

iOS Safari refuses to grant getUserMedia over plain HTTP, so the proxy
needs HTTPS even for LAN/Tailscale use. A fresh cert lives in
``state/certs/`` (gitignored). Paul will get a browser trust prompt the
first time he opens it on each device; after that Safari remembers the
exception.

We use the ``cryptography`` package that's already on the requirements
list — no new dependency.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import socket
import ssl
from pathlib import Path
from typing import Optional

logger = logging.getLogger("renee.client.cert_manager")

CERT_NAME = "proxy.pem"
KEY_NAME = "proxy.key"


def ensure_self_signed_cert(
    cert_dir: Path,
    *,
    extra_hosts: Optional[list[str]] = None,
) -> ssl.SSLContext:
    """Return an SSL context using cert + key in ``cert_dir``.

    If either file is missing a new self-signed cert is minted first.
    ``extra_hosts`` lets callers thread in the Tailscale IP and
    hostnames so the browser's SAN check succeeds.
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / CERT_NAME
    key_path = cert_dir / KEY_NAME
    if not (cert_path.exists() and key_path.exists()):
        logger.info("generating self-signed cert at %s", cert_path)
        _generate(cert_path, key_path, extra_hosts=extra_hosts or [])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx


def _generate(
    cert_path: Path,
    key_path: Path,
    *,
    extra_hosts: list[str],
) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "renee.local")]
    )

    san = _build_san(extra_hosts)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.timezone.utc))
        .not_valid_after(
            _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=3650)
        )
        .add_extension(san, critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        key_path.chmod(0o600)
    except OSError:
        pass  # Windows — no POSIX perms, ignore silently.


def _build_san(extra_hosts: list[str]):
    from cryptography import x509

    entries: list = [
        x509.DNSName("renee.local"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    seen_dns: set[str] = {"renee.local", "localhost"}
    seen_ip: set[str] = {"127.0.0.1"}

    for host in extra_hosts + _local_hostnames():
        host = host.strip()
        if not host:
            continue
        try:
            ip = ipaddress.ip_address(host)
            key = str(ip)
            if key not in seen_ip:
                entries.append(x509.IPAddress(ip))
                seen_ip.add(key)
        except ValueError:
            if host not in seen_dns:
                entries.append(x509.DNSName(host))
                seen_dns.add(host)
    return x509.SubjectAlternativeName(entries)


def _local_hostnames() -> list[str]:
    out: list[str] = []
    try:
        hostname = socket.gethostname()
        if hostname:
            out.append(hostname)
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip not in out:
                out.append(ip)
    except Exception:
        pass
    return out
