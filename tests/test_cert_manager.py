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


def test_cert_is_valid_at_least_365_days(tmp_path: Path):
    import datetime as _dt

    from cryptography import x509

    ensure_self_signed_cert(tmp_path)
    cert = x509.load_pem_x509_certificate((tmp_path / "proxy.pem").read_bytes())
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert lifetime >= _dt.timedelta(days=365), (
        f"cert lifetime {lifetime} is shorter than one year"
    )


def test_cert_key_is_at_least_2048_rsa(tmp_path: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    ensure_self_signed_cert(tmp_path)
    cert = x509.load_pem_x509_certificate((tmp_path / "proxy.pem").read_bytes())
    pk = cert.public_key()
    if isinstance(pk, rsa.RSAPublicKey):
        assert pk.key_size >= 2048
    elif isinstance(pk, ec.EllipticCurvePublicKey):
        assert pk.curve.key_size >= 256
    else:
        pytest.fail(f"unexpected key type {type(pk).__name__}")


def test_san_includes_machine_hostname(tmp_path: Path):
    """The hostname must appear in SAN so `https://matrix.local/` works."""
    import socket as _s

    from cryptography import x509

    hostname = _s.gethostname()
    if not hostname:
        pytest.skip("no hostname to verify")
    ensure_self_signed_cert(tmp_path)
    cert = x509.load_pem_x509_certificate((tmp_path / "proxy.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    dns_names = list(san.get_values_for_type(x509.DNSName))
    assert hostname in dns_names, (
        f"machine hostname {hostname!r} missing from SAN {dns_names}"
    )
