"""Claude 메시지 RPC 경로 — 인라인 텍스트가 검사·마스킹된다.

2026-09-23~24 실측: 새 대화의 텍스트 파일·손으로 친 프롬프트가 파일 업로드가 아니라
이 protobuf RPC 로 나가 검사 밖으로 샜다. 캡처 5건을 디코드해 필드를 특정했다:
  2.3     손으로 친 메시지 텍스트
  2.15.4  인라인된 텍스트 파일 내용
필드 2 가 없는 액션(메타 핑)은 사용자 콘텐츠가 없어 통과한다.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow, tutils  # noqa: E402

from app.adapters.proxy import protobuf as pb  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon, CLAUDE_MESSAGE_RPC_PATHS  # noqa: E402

RPC = CLAUDE_MESSAGE_RPC_PATHS[0]


def _tag(num, wt):
    return pb._encode_varint((num << 3) | wt)


def _vi(num, n):
    return _tag(num, 0) + pb._encode_varint(n)


def _ld(num, data):
    return _tag(num, 2) + pb._encode_varint(len(data)) + data


def _flow(body, ctype="application/proto"):
    f = tflow.tflow(req=tutils.treq(method="POST", host="claude.ai", path=RPC, content=body))
    f.request.headers["content-type"] = ctype
    return f


def _run(c):
    return asyncio.run(c)


def _fake_pipeline(mask_map):
    """run_pipeline 을 대신한다 — 입력 텍스트를 mask_map 으로 치환."""
    async def fake(*, text=None, **kw):
        m = mask_map.get(text, text)
        pii = [{"type": "ID", "start": 0, "end": 1}] if m != text else []
        return {"maskedText": m, "piiItems": pii, "injectionItems": [], "blocked": False,
                "stats": {"piiCount": len(pii), "injectionCount": 0}}
    return fake


# 실제 캡처와 같은 배치: 최상위 2 = 전송, 2.3 = 프롬프트, 2.15 = 파일(2.15.4=내용)
def _send_with(typed=None, file_content=None, filename="test.txt"):
    parts = [_ld(1, _ld(2, b"conv-uuid")), _ld(3, b"1803"[:1])]  # 1.x 메타(대충)
    f2 = b""
    f2 += _ld(1, b"11111111-1111-1111-1111-111111111111")  # 2.1 uuid
    f2 += _ld(2, b"22222222-2222-2222-2222-222222222222")  # 2.2 uuid
    if typed is not None:
        f2 += _ld(3, typed.encode())                        # 2.3 typed text
    f2 += _ld(7, _ld(2, b"claude-opus-5-5"))                # 2.7.2 model name (metadata)
    if file_content is not None:
        block = _ld(1, filename.encode()) + _vi(2, 4130) + _ld(3, b"text/plain") + _ld(4, file_content.encode())
        f2 += _ld(15, block)                                # 2.15 file
    f2 += _ld(12, b"Asia/Seoul") + _ld(13, b"ko-KR")        # metadata
    return _ld(2, f2)


def test_손으로_친_텍스트를_마스킹한다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    monkeypatch.setattr(orch, "run_pipeline",
                        _fake_pipeline({"주민번호 900101-1234567": "주민번호 [마스킹]"}))
    a = CampfireAddon()

    async def go():
        f = _flow(_send_with(typed="주민번호 900101-1234567"))
        task = asyncio.create_task(a.request(f))
        await asyncio.sleep(0.05)
        from app.adapters.proxy.decision import broker as B
        pend = B.list_pending()
        assert len(pend) == 1, "검토 요청이 한 번 떠야 한다"
        B.resolve(pend[0]["id"], "send_masked")
        await task
        return f
    f = _run(go())
    assert f.response is None, "마스킹 후 통과해야 한다(차단 아님)"
    out = pb.decode(f.request.content)
    got = {p: fld for p, fld in out.walk()}
    assert got[(2, 3)].value.decode() == "주민번호 [마스킹]"
    assert "900101" not in f.request.content.decode("utf-8", "replace")
    # 메타데이터는 그대로
    assert got[(2, 7, 2)].value.decode() == "claude-opus-5-5"


def test_인라인_파일_내용도_마스킹한다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    monkeypatch.setattr(orch, "run_pipeline",
                        _fake_pipeline({"카드 4111-1111-1111-1111": "카드 [마스킹]",
                                        "요약해줘": "요약해줘"}))

    async def go():
        a = CampfireAddon()
        f = _flow(_send_with(typed="요약해줘", file_content="카드 4111-1111-1111-1111"))
        task = asyncio.create_task(a.request(f))
        await asyncio.sleep(0.05)
        from app.adapters.proxy.decision import broker as B
        assert len(B.list_pending()) == 1, "파일+프롬프트인데도 검토는 한 번"
        B.resolve(B.list_pending()[0]["id"], "send_masked")
        await task
        return f
    f = _run(go())
    out = pb.decode(f.request.content)
    got = {p: fld for p, fld in out.walk()}
    assert got[(2, 15, 4)].value.decode() == "카드 [마스킹]"
    assert "4111-1111" not in f.request.content.decode("utf-8", "replace")
    assert got[(2, 15, 1)].value.decode() == "test.txt"  # 파일명 보존
    assert got[(2, 15, 3)].value.decode() == "text/plain"


def test_필드2_없는_액션은_통과한다():
    # 실제 120바이트 비전송 액션 모양: 1.x + 15.x, 필드 2 없음
    body = _ld(1, _ld(2, b"conv")) + _ld(14, b"ko-KR") + _ld(15, _ld(1, _ld(7, _ld(2, b"claude-opus-5-5"))))
    f = _flow(body)
    _run(CampfireAddon().request(f))
    assert f.response is None, "메타 핑을 막으면 대화가 끊긴다"


def test_취소하면_차단():
    async def go():
        a = CampfireAddon()
        import app.core.pipeline.orchestrator as orch
        orig = orch.run_pipeline
        orch.run_pipeline = _fake_pipeline({"x 900101-1234567": "x [마스킹]"})
        try:
            f = _flow(_send_with(typed="x 900101-1234567"))
            task = asyncio.create_task(a.request(f))
            await asyncio.sleep(0.05)
            from app.adapters.proxy.decision import broker as B
            B.resolve(B.list_pending()[0]["id"], "cancel")
            await task
            return f
        finally:
            orch.run_pipeline = orig
    f = _run(go())
    assert f.response is not None and f.response.status_code == 403


def test_봉투형식은_차단():
    f = _flow(b"\x00\x00\x00\x00\x05hello", ctype="application/connect+proto")
    _run(CampfireAddon().request(f))
    assert f.response is not None and f.response.status_code == 403


def test_못_읽는_본문은_차단():
    f = _flow(b"\xff\xff\xff\xff")
    _run(CampfireAddon().request(f))
    assert f.response is not None and f.response.status_code == 403


def test_다른_RPC_는_안_건드린다():
    f = tflow.tflow(req=tutils.treq(
        method="POST", host="claude.ai",
        path="/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/StreamTimeline",
        content=_ld(2, _ld(3, "무언가".encode()))))
    f.request.headers["content-type"] = "application/proto"
    _run(CampfireAddon().request(f))
    assert f.response is None
