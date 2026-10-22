"""Campfire 전용 프록시 CA — 이름 제한으로 AI 도메인 밖에서는 쓸모가 없어야 한다.

2026-10-22 실측(이 PC, 사용자가 CurrentUser\\Root 에 신뢰 등록): 같은 CA 로 서명한 인증서가
chatgpt.com·claude.ai·grok.com 은 Chrome 에서 열리고 www.google.com·github.com 은 ERR_CERT_INVALID,
certutil -user -verify 는 www.google.com 에 CERT_TRUST_HAS_NOT_PERMITTED_NAME_CONSTRAINT 를 냈다.
여기서는 같은 판정을 cryptography 검증기(신뢰 앵커의 이름 제한까지 본다)로 지킨다.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("mitmproxy")

from cryptography import x509  # noqa: E402
from cryptography.x509.verification import PolicyBuilder, Store  # noqa: E402
from mitmproxy import certs  # noqa: E402

from app.adapters.proxy import ca  # noqa: E402

NAMES = ["chatgpt.com", "claude.ai", "perplexity.ai"]


def _root(confdir):
    return x509.load_pem_x509_certificate((confdir / "mitmproxy-ca-cert.pem").read_bytes())


def _leaf(confdir, passphrase, host):
    store = certs.CertStore.from_store(confdir, ca.BASENAME, 2048, passphrase)
    return x509.load_pem_x509_certificate(store.get_cert(host, [x509.DNSName(host)]).cert.to_pem())


def _verifies(root, leaf, host) -> bool:
    try:
        PolicyBuilder().store(Store([root])).build_server_verifier(x509.DNSName(host)).verify(leaf, [])
        return True
    except Exception:
        return False


def test_이름_제한은_짧은_이름이_덮는_것을_뺀다():
    assert ca.constraint_names(["www.perplexity.ai", "perplexity.ai", "Claude.ai.", "suggest.perplexity.ai"]) \
        == ["claude.ai", "perplexity.ai"]


def test_AI_도메인_밖에서는_CA_가_쓸모없다(tmp_path):
    passphrase = ca.ensure(tmp_path, NAMES)
    root = _root(tmp_path)

    nc = root.extensions.get_extension_for_class(x509.NameConstraints)
    assert nc.critical
    assert sorted(n.value for n in nc.value.permitted_subtrees) == NAMES
    assert {n.value for n in nc.value.excluded_subtrees} == {
        ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")}
    bc = root.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca and bc.value.path_length == 0
    assert root.not_valid_after_utc - root.not_valid_before_utc <= timedelta(days=ca.VALID_DAYS + 1)

    for host in ("chatgpt.com", "www.chatgpt.com", "claude.ai", "www.perplexity.ai"):
        assert _verifies(root, _leaf(tmp_path, passphrase, host), host), host
    for host in ("www.google.com", "github.com", "chatgpt.com.evil.example"):
        assert not _verifies(root, _leaf(tmp_path, passphrase, host), host), f"{host} 가 통과했다"


def test_개인키는_암호로_잠기고_mitmproxy_가_그_암호로_연다(tmp_path):
    passphrase = ca.ensure(tmp_path, NAMES)
    raw = (tmp_path / "mitmproxy-ca.pem").read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" in raw and b"BEGIN PRIVATE KEY" not in raw
    with pytest.raises(Exception):
        certs.CertStore.from_store(tmp_path, ca.BASENAME, 2048, b"wrong")
    leaf = _leaf(tmp_path, passphrase, "chatgpt.com")
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [n.value for n in sans] == ["chatgpt.com"]


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 는 Windows 에만 있다")
def test_Windows_는_암호를_DPAPI_로_보관한다(tmp_path):
    passphrase = ca.ensure(tmp_path, NAMES)
    stored = (tmp_path / "campfire-ca.secret").read_bytes()
    assert passphrase not in stored, "개인키 암호가 평문으로 남았다"
    assert ca._dpapi(stored, protect=False) == passphrase


def test_도메인이_바뀌거나_만료가_가까우면_새로_만들고_아니면_그대로(tmp_path):
    now = datetime.now(timezone.utc)
    ca.ensure(tmp_path, NAMES, now)
    sha1 = json.loads((tmp_path / "campfire-ca.json").read_text(encoding="utf-8"))["sha1"]
    fp = lambda: json.loads((tmp_path / "campfire-ca.json").read_text(encoding="utf-8"))["sha1"]  # noqa: E731

    ca.ensure(tmp_path, NAMES, now + timedelta(days=100))
    assert fp() == sha1, "멀쩡한 CA 를 새로 만들었다 — 사용자가 매번 다시 신뢰해야 한다"

    ca.ensure(tmp_path, [*NAMES, "grok.com"], now)
    assert fp() != sha1, "검사 대상 도메인이 늘었는데 예전 CA 를 그대로 쓴다"

    renewed = fp()
    ca.ensure(tmp_path, [*NAMES, "grok.com"], now + timedelta(days=ca.VALID_DAYS - ca.RENEW_BEFORE_DAYS + 1))
    assert fp() != renewed, "만료가 가까운데 새로 만들지 않았다"

    (tmp_path / "campfire-ca.secret").unlink()
    again = fp()
    ca.ensure(tmp_path, [*NAMES, "grok.com"], now + timedelta(days=ca.VALID_DAYS))
    assert fp() != again, "개인키 암호를 잃었는데 그 CA 를 계속 쓰려 했다"


def test_프록시는_Campfire_CA_로_뜨고_원래_인증서의_이름을_베끼지_않는다(monkeypatch, tmp_path):
    from app import config
    from app.adapters import proxy

    monkeypatch.setattr(config, "PROXY_CA_DIR", tmp_path)
    monkeypatch.setattr(config, "PROXY_PORT", 48297)

    async def go():
        assert await proxy.start(force=True) is True
        try:
            opts = proxy._master.options
            return opts.upstream_cert, opts.confdir
        finally:
            await proxy.stop()

    upstream_cert, confdir = asyncio.run(go())
    assert upstream_cert is False
    assert confdir == str(tmp_path)
    assert (tmp_path / "mitmproxy-ca-cert.cer").exists() and (tmp_path / "mitmproxy-ca-cert.pem").exists()


def test_예전_CA_는_신뢰돼_있을_때만_알린다(monkeypatch, tmp_path):
    from app.adapters import proxy

    old = tmp_path / "mitmproxy-ca-cert.cer"
    ca.ensure(tmp_path / "x", NAMES)
    old.write_bytes((tmp_path / "x" / "mitmproxy-ca-cert.cer").read_bytes())
    monkeypatch.setattr(proxy, "_legacy_ca_path", lambda: old)

    monkeypatch.setattr(proxy, "_trusted", lambda p: False)
    assert proxy.legacy_ca() is None
    monkeypatch.setattr(proxy, "_trusted", lambda p: True)
    got = proxy.legacy_ca()
    assert got["sha1"] == proxy._cert_sha1(old) and len(got["sha1"]) == 40
    old.unlink()
    assert proxy.legacy_ca() is None
