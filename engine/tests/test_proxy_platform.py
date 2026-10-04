from __future__ import annotations

from types import SimpleNamespace

from app.adapters import proxy


def test_macos_ca_trust_uses_security_verify_cert(tmp_path):
    cert = tmp_path / "mitmproxy-ca-cert.pem"
    cert.write_text("dummy")
    fingerprint = proxy._cert_sha1(cert)
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=f"SHA-1 hash: {fingerprint}\n".encode())

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


def test_macos_ca_trust_requires_cert_in_keychain(tmp_path):
    """신뢰 설정은 있는데 인증서가 키체인에 없으면 Chrome 이 발급자를 못 찾는다.

    실사용 맥에서 verify-cert 는 0 인데 find-certificate 가 0건이었고, 프록시를 켜자
    claude.ai 가 ERR_CERT_AUTHORITY_INVALID 로 막혔다. 그 상태를 신뢰로 보면 안 된다.
    """
    cert = tmp_path / "mitmproxy-ca-cert.pem"
    cert.write_text("dummy")

    def run(args, **_kwargs):
        if args[1] == "verify-cert":
            return SimpleNamespace(returncode=0, stdout=b"")
        return SimpleNamespace(returncode=0, stdout=b"SHA-1 hash: 0123456789ABCDEF0123456789ABCDEF01234567\n")

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
