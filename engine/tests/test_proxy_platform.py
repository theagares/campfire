from __future__ import annotations

from types import SimpleNamespace

from app.adapters import proxy


def test_macos_ca_trust_uses_security_verify_cert(tmp_path):
    cert = tmp_path / "mitmproxy-ca-cert.pem"
    cert.write_text("dummy")
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    assert proxy._macos_ca_trusted(cert, run=run) is True
    args, kwargs = seen[0]
    assert args[:2] == ["/usr/bin/security", "verify-cert"]
    assert ["-c", str(cert)] == args[2:4]
    assert "-p" in args and "ssl" in args and "-l" in args and "-L" in args
    assert kwargs["timeout"] == 10


def test_macos_ca_trust_rejects_failed_verification(tmp_path):
    cert = tmp_path / "mitmproxy-ca-cert.pem"
    cert.write_text("dummy")

    def run(_args, **_kwargs):
        return SimpleNamespace(returncode=1)

    assert proxy._macos_ca_trusted(cert, run=run) is False


def test_proxy_status_supports_macos(monkeypatch, tmp_path):
    cert = tmp_path / "mitmproxy-ca-cert.pem"
    monkeypatch.setattr(proxy.sys, "platform", "darwin")
    monkeypatch.setattr(proxy, "ca_cert_path", lambda: str(cert))
    monkeypatch.setattr(proxy, "ca_trusted", lambda: False)

    status = proxy.status()

    assert status["supported"] is True
    assert status["platform"] == "darwin"
    assert status["caPath"].endswith("mitmproxy-ca-cert.pem")
