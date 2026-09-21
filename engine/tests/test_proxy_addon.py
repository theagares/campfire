"""애드온 분기 — "조용히 통과" 가 없는지만 본다.

이 파일이 지키는 것은 하나다: **판단하지 않은 바이트가 사이트로 나가지 않는다.**
마스킹 품질은 파이프라인 테스트가 본다. 여기서는 흐름만 본다.

run_pipeline 은 부르지 않는다(모델 로드가 붙는다) — `_scan_and_decide` 를
갈아끼워 사람 판단 결과만 흉내 낸다.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow, tutils  # noqa: E402

from app.adapters.proxy import addon as addon_mod  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon, ChatGptUpload, _ORIGINAL  # noqa: E402


def _req(host: str, path: str, method: str = "POST", content: bytes = b"x"):
    f = tflow.tflow(req=tutils.treq(method=method, host=host, path=path, content=content))
    f.request.headers["content-type"] = "application/json"
    return f


def _run(coro):
    return asyncio.run(coro)


# ── Grok: 다룰 수 없는 형식은 통과가 아니라 차단 ────────────────────────────


def test_grok_업로드는_차단된다():
    """Grok 은 JSON+base64 라 아직 못 고친다.

    예전엔 호스트 목록에 없어서 **그냥 통과**했다 — 원문이 그대로 나갔다는 뜻이다.
    못 다루는 것과 흘려보내는 것은 다르다.
    """
    a = CampfireAddon()
    f = _req("grok.com", "/rest/app-chat/upload-file", content=b'{"content":"BASE64"}')
    _run(a.request(f))

    assert f.response is not None, "차단되지 않았다 — 원문이 그대로 나간다"
    assert f.response.status_code == 403
    assert b"campfire_blocked" in f.response.content
    # 본문은 건드리지 않는다. 어차피 안 보낸다.
    assert f.request.content == b'{"content":"BASE64"}'


def test_grok_의_다른_요청은_건드리지_않는다():
    """업로드 엔드포인트만 막는다. 전부 막으면 사이트가 죽는다."""
    a = CampfireAddon()
    f = _req("grok.com", "/rest/app-chat/conversations")
    _run(a.request(f))
    assert f.response is None


def test_gemini_업로드는_차단된다():
    """Gemini 는 push.clients6.google.com 으로 raw 본문을 올린다(2026-09-21 실측).

    content-type 이 x-www-form-urlencoded 라고 적혀 있지만 본문 전체가 파일이라
    multipart 처리가 안 먹는다. 호스트를 MULTIPART_HOSTS 에 넣어 봐야 그냥
    통과했다 — 그게 원래 구멍이었다.
    """
    a = CampfireAddon()
    f = _req("push.clients6.google.com", "/upload/", content=b"\x89PNG" + b"x" * 5000)
    f.request.headers["content-type"] = "application/x-www-form-urlencoded;charset=utf-8"
    _run(a.request(f))

    assert f.response is not None, "차단되지 않았다 — 원문이 그대로 나간다"
    assert f.response.status_code == 403


def test_모르는_호스트는_통과한다():
    """게이트웨이는 허용 목록만 본다 — 인터넷 전체를 검사하지 않는다."""
    a = CampfireAddon()
    f = _req("example.com", "/upload")
    _run(a.request(f))
    assert f.response is None


# ── ChatGPT: PUT 재시도가 통과하지 않는지 ───────────────────────────────────


def _put_flow(path: str, content: bytes):
    return tflow.tflow(
        req=tutils.treq(method="PUT", host="files.oaiusercontent.com", path=path, content=content)
    )


def _armed(a: CampfireAddon, key: str, declared: int = 1024) -> ChatGptUpload:
    promise = ChatGptUpload(declared=declared, masked_name="a_masked.md", original_name="a.docx")
    a._chatgpt[key] = promise
    return promise


def test_PUT_재시도는_같은_결과를_쓰고_다시_묻지_않는다():
    """예전엔 첫 PUT 에서 약속값을 pop 해서, 재시도가 분기를 못 타고 **원본으로
    나갔다.** 업로드 재시도는 흔한 일이라 그냥 두면 실제로 밟힌다."""
    a = CampfireAddon()
    _armed(a, "/files/abc/raw")
    asked = []

    async def fake(*, data, file_name, mime, host):
        asked.append(file_name)
        return b"# masked"

    a._scan_and_decide = fake  # type: ignore[assignment]

    f1 = _put_flow("/files/abc/raw", b"ORIGINAL-SECRET")
    _run(a.request(f1))
    assert f1.response is None
    assert f1.request.content.startswith(b"# masked")
    assert len(f1.request.content) == 1024      # 선언값에 정확히 맞춘다

    # 재시도: 사이트가 같은 PUT 을 다시 보낸다.
    f2 = _put_flow("/files/abc/raw", b"ORIGINAL-SECRET")
    _run(a.request(f2))
    assert f2.response is None
    assert f2.request.content == f1.request.content, "재시도에서 원본이 나갔다"
    assert b"ORIGINAL-SECRET" not in f2.request.content
    assert asked == ["a.docx"], "재시도인데 사람에게 또 물었다"


def test_한번_막은_업로드는_재시도도_막는다():
    a = CampfireAddon()
    _armed(a, "/files/xyz/raw")

    async def refuse(*, data, file_name, mime, host):
        return None  # 사람이 '보내지 않음' 을 골랐다

    a._scan_and_decide = refuse  # type: ignore[assignment]

    f1 = _put_flow("/files/xyz/raw", b"ORIGINAL-SECRET")
    _run(a.request(f1))
    assert f1.response.status_code == 403

    f2 = _put_flow("/files/xyz/raw", b"ORIGINAL-SECRET")
    _run(a.request(f2))
    assert f2.response is not None and f2.response.status_code == 403


def test_마스킹본이_선언값보다_크면_차단하고_재시도도_막는다():
    """자르면 사용자가 모르는 채로 내용이 사라진다. 조용한 손실보다 실패가 낫다."""
    a = CampfireAddon()
    _armed(a, "/files/big/raw", declared=8)

    async def big(*, data, file_name, mime, host):
        return b"x" * 100

    a._scan_and_decide = big  # type: ignore[assignment]

    f1 = _put_flow("/files/big/raw", b"ORIGINAL")
    _run(a.request(f1))
    assert f1.response.status_code == 403

    f2 = _put_flow("/files/big/raw", b"ORIGINAL")
    _run(a.request(f2))
    assert f2.response.status_code == 403


def test_원본_전송을_골라도_선언값에_맞춰_채운다():
    """등록은 이미 N' 으로 고쳐 버렸다. 원본 크기 그대로 보내면 사이트가 거부한다."""
    a = CampfireAddon()
    _armed(a, "/files/orig/raw", declared=64)

    async def original(*, data, file_name, mime, host):
        return _ORIGINAL

    a._scan_and_decide = original  # type: ignore[assignment]

    f = _put_flow("/files/orig/raw", b"ORIGINAL")
    _run(a.request(f))
    assert f.response is None
    assert f.request.content.startswith(b"ORIGINAL")
    assert len(f.request.content) == 64


def test_추적_목록은_무한히_자라지_않는다():
    """재시도용으로 남겨 두는 항목이라 스스로 줄지 않는다."""
    a = CampfireAddon()
    for i in range(addon_mod._MAX_TRACKED_UPLOADS + 20):
        f = tflow.tflow(
            req=tutils.treq(method="POST", host="chatgpt.com", path="/backend-api/files")
        )
        f.metadata["campfire_upload"] = ChatGptUpload(
            declared=10, masked_name="m.md", original_name="o.docx"
        )
        f.response = tutils.tresp(
            content=('{"upload_url":"https://x.oaiusercontent.com/files/%d/raw?sig=1"}' % i).encode()
        )
        a.response(f)
    assert len(a._chatgpt) == addon_mod._MAX_TRACKED_UPLOADS
