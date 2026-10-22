"""
app/adapters/proxy/ca.py
프록시가 쓰는 Campfire 전용 루트 CA.

mitmproxy 가 처음 뜰 때 ~/.mitmproxy 에 알아서 만드는 CA 는 **모든 사이트**에 쓸 수 있고(이름 제한
없음), 10년짜리이며, 개인키가 사용자 폴더에 평문으로 남는다. 사용자가 그 CA 를 신뢰한 PC 에서
키가 새면 은행·메일까지 이 PC 의 모든 HTTPS 를 위장할 수 있다. 그래서 직접 만든다.

  - 이름 제한(Name Constraints, critical): 검사 대상 AI 도메인만 허용하고 IP 주소는 전부 뺀다.
    키가 새도 AI 사이트 밖에서는 브라우저가 이 CA 로 서명한 인증서를 거부한다.
  - 2년. 만료 30일 전이거나 검사 대상 도메인이 바뀌면 새로 만든다 — 그러면 사용자가 다시 신뢰해야
    한다(앱이 등록 명령을 다시 보여 준다).
  - 개인키는 무작위 암호로 잠근다. Windows 는 그 암호를 DPAPI(사용자 계정에 묶인 암호화)로 보관한다.
    ponytail: DPAPI 는 같은 사용자로 도는 프로그램은 막지 못한다 — 막는 건 파일만 복사해 가는 경우다.
    진짜 방어선은 이름 제한이다. 다른 OS 는 사용자만 읽는 파일(0600)로 둔다.

mitmproxy 는 confdir 의 mitmproxy-ca.pem(개인키+인증서)을 cert_passphrase 로 풀어 쓴다.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

COMMON_NAME = "Campfire Local Proxy CA"
BASENAME = "mitmproxy"  # mitmproxy 가 confdir 에서 찾는 이름이라 바꿀 수 없다
VALID_DAYS = 730
RENEW_BEFORE_DAYS = 30
_META = "campfire-ca.json"
_SECRET = "campfire-ca.secret"


def cert_file(confdir: Path) -> Path:
    """사용자가 신뢰 저장소에 넣는 인증서 파일. macOS security 는 PEM, Windows certutil 은 DER."""
    return confdir / (f"{BASENAME}-ca-cert.pem" if sys.platform == "darwin" else f"{BASENAME}-ca-cert.cer")


def constraint_names(hosts) -> list[str]:
    """이름 제한에 넣을 도메인. 더 짧은 이름이 덮는 것은 뺀다(perplexity.ai 가 www.perplexity.ai 를 덮는다)."""
    names = sorted({h.strip().lower().rstrip(".") for h in hosts if h and h.strip()})
    return [n for n in names if not any(n != m and n.endswith("." + m) for m in names)]


def ensure(confdir: Path, names: list[str], now: datetime | None = None) -> bytes:
    """CA 가 없거나 낡았으면 새로 만든다. mitmproxy 에 넘길 개인키 암호를 돌려준다.

    있는 CA 의 개인키를 그 암호로 열 수 없으면(DPAPI 키가 바뀜 — 관리자가 비밀번호를 강제로 바꾼 경우,
    다른 계정에서 폴더를 옮겨 옴, 파일 손상) 새로 만든다. 그러지 않으면 프록시가 영영 안 켜진다.
    새 CA 는 사용자가 한 번 다시 신뢰하면 된다.
    """
    now = now or datetime.now(timezone.utc)
    if _current(confdir, names, now):
        try:
            return _opened_secret(confdir)
        except (OSError, ValueError, TypeError):
            pass
    _create(confdir, names, now)
    return _opened_secret(confdir)


def _opened_secret(confdir: Path) -> bytes:
    """개인키 암호를 읽고, 그 암호로 개인키가 실제로 열리는지까지 확인한다."""
    from cryptography.hazmat.primitives import serialization

    passphrase = _read_secret(confdir)
    serialization.load_pem_private_key((confdir / f"{BASENAME}-ca.pem").read_bytes(), passphrase)
    return passphrase


def _current(confdir: Path, names: list[str], now: datetime) -> bool:
    try:
        meta = json.loads((confdir / _META).read_text(encoding="utf-8"))
        not_after = datetime.fromisoformat(meta["notAfter"])
    except (OSError, ValueError, KeyError):
        return False
    files = (confdir / f"{BASENAME}-ca.pem", cert_file(confdir), confdir / _SECRET)
    return (
        meta.get("names") == names
        and not_after - now > timedelta(days=RENEW_BEFORE_DAYS)
        and all(f.exists() for f in files)
    )


def _create(confdir: Path, names: list[str], now: datetime) -> None:
    # cryptography 는 mitmproxy 와 함께 깔린다(선택 의존성). 경로만 묻는 쪽은 없어도 되게 여기서 부른다.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    confdir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Campfire"),
    ])
    not_after = now + timedelta(days=VALID_DAYS)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.NameConstraints(
            permitted_subtrees=[x509.DNSName(n) for n in names],
            excluded_subtrees=[
                x509.IPAddress(ipaddress.ip_network("0.0.0.0/0")),
                x509.IPAddress(ipaddress.ip_network("::/0")),
            ],
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )

    passphrase = base64.b64encode(secrets.token_bytes(32))
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    # 암호를 먼저 쓴다 — 키만 남고 암호를 잃으면 그 CA 는 다시 못 연다(그때는 새로 만든다).
    _write_secret(confdir, passphrase)
    _write_private(confdir / f"{BASENAME}-ca.pem", key_pem + cert_pem)
    (confdir / f"{BASENAME}-ca-cert.pem").write_bytes(cert_pem)
    (confdir / f"{BASENAME}-ca-cert.cer").write_bytes(cert.public_bytes(serialization.Encoding.DER))
    (confdir / _META).write_text(json.dumps({
        "names": names,
        "notAfter": not_after.isoformat(),
        "sha1": cert.fingerprint(hashes.SHA1()).hex().upper(),
    }, ensure_ascii=False), encoding="utf-8")


def _write_private(path: Path, data: bytes) -> None:
    """사용자만 읽는 파일로 쓴다(POSIX 0600). Windows 는 사용자 폴더의 기본 권한을 따른다."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _write_secret(confdir: Path, passphrase: bytes) -> None:
    data = _dpapi(passphrase, protect=True) if sys.platform == "win32" else passphrase
    _write_private(confdir / _SECRET, data)


def _read_secret(confdir: Path) -> bytes:
    data = (confdir / _SECRET).read_bytes()
    return _dpapi(data, protect=False) if sys.platform == "win32" else data


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Windows DPAPI(CryptProtectData/CryptUnprotectData) — 현재 사용자 계정에 묶어 잠그고 푼다."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # 마지막 인자 앞 0x1 = CRYPTPROTECT_UI_FORBIDDEN — 엔진은 화면이 없다.
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):
        raise OSError(f"DPAPI {'잠그기' if protect else '풀기'} 실패: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)
