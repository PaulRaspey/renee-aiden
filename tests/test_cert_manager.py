"""Tests for the self-signed certificate helper."""
from __future__ import annotations

import ssl
from pathlib import Path

from src.client.cert_manager import ensure_self_signed_cert


def test_ensure_self_signed_cert_generates_cert_and_key(tmp_path: Path):
    ctx = ensure_self_signed_cert(tmp_path)
    assert isinstance(ctx, ssl.SSLContext)
    assert (tmp_path / "proxy.pem").is_file()
    assert (tmp_path / "proxy.key").is_file()


def test_ensure_self_signed_cert_reuses_existing_files(tmp_path: Path):
    """A second call must not regenerate — cert mtime should be unchanged."""
    ctx1 = ensure_self_signed_cert(tmp_path)
    pem = tmp_path / "proxy.pem"
    mtime_before = pem.stat().st_mtime_ns
    ctx2 = ensure_self_signed_cert(tmp_path)
    assert pem.stat().st_mtime_ns == mtime_before
    assert isinstance(ctx1, ssl.SSLContext)
    assert isinstance(ctx2, ssl.SSLContext)


def test_generated_cert_includes_extra_host_in_san(tmp_path: Path):
    """The SAN must pick up a Tailscale IP so iOS doesn't reject it outright."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    ensure_self_signed_cert(tmp_path, extra_hosts=["100.64.0.5", "renee.tail"])
    pem = (tmp_path / "proxy.pem").read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    san = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    dns_names = list(san.get_values_for_type(x509.DNSName))
    ips = [str(n) for n in san.get_values_for_type(x509.IPAddress)]
    assert "renee.tail" in dns_names
    assert "100.64.0.5" in ips
    # Defaults still present.
    assert "localhost" in dns_names
    assert "127.0.0.1" in ips
    # Sanity: the issued cert is parseable and self-signed.
    assert cert.issuer == cert.subject
    assert cert.public_key() is not None
    _ = serialization  # silence unused
