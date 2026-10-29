"""Gemini 프롬프트 — WebChannel processSession 의 reqN___data__(base64 protobuf) 2.9.1 을 검사·마스킹한다.

2026-10-29 맥 실측(확장 끔·프록시 켬): 프롬프트가 PAC 밖 호스트(geminiweb-pa.clients6.google.com)로 QUIC 직결해
프록시가 못 봤고, 저장본에 원문이 남았다. 아래 req0 값은 그때 캡처한 "CFPROBE 캡처 시험입니다" 그대로다.
"""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import quote

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow, tutils  # noqa: E402

import app.adapters.proxy.addon as addon_mod  # noqa: E402
from app.adapters.proxy import protobuf as pb  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon  # noqa: E402

CAPTURED = "EiQIAUogCh5DRlBST0JFIOy6oeyymCDsi5ztl5jsnoXri4jri6Q="
PATH = "/v1/processSession?VER=8&gsessionid=g&SID=s&RID=3&AID=1&zx=z&t=1"


def _flow(body: str):
    f = tflow.tflow(req=tutils.treq(method="POST", host=addon_mod.GEMINI_PROMPT_HOST, path=PATH,
                                    content=body.encode()))
    f.request.headers["content-type"] = "application/x-www-form-urlencoded"
    return f


def _ld(num, data):
    return pb._encode_varint((num << 3) | 2) + pb._encode_varint(len(data)) + data


def _turn(text: str) -> bytes:
    """사용자 턴 {2: {1: 1, 9: {1: text}}} — 캡처와 같은 모양."""
    return _ld(2, pb._encode_varint(1 << 3) + pb._encode_varint(1) + _ld(9, _ld(1, text.encode())))


def _prompt_of(body: str) -> str:
    from urllib.parse import unquote

    data = dict(p.split("=", 1) for p in body.split("&"))["req0___data__"]
    msg = pb.decode(base64.b64decode(unquote(data)))
    return next(f.value.decode() for path, f in msg.walk() if path == (2, 9, 1))


def _fake(mask_map, seen):
    async def run(*, text=None, direct_prompt=False, scan_injection=True, **_kw):
        seen.append((text, direct_prompt, scan_injection))
        m = mask_map.get(text, text)
        pii = [{"type": "PHONE", "start": 0, "end": 1}] if m != text else []
        return {"maskedText": m, "piiItems": pii, "injectionItems": [], "forcedMaskItems": [], "blocked": False,
                "scanStatus": "ok", "truncated": False, "stats": {"piiCount": len(pii), "injectionCount": 0}}
    return run


@pytest.fixture
def decide_masked(monkeypatch):
    async def wait(**_kw):
        return "send_masked"
    monkeypatch.setattr(addon_mod.broker, "wait", wait)


def test_캡처한_프롬프트를_읽어_가린_글로_다시_싼다(monkeypatch, decide_masked):
    import app.core.pipeline.orchestrator as orch
    seen = []
    monkeypatch.setattr(orch, "run_pipeline", _fake({"CFPROBE 캡처 시험입니다": "[가림] 시험입니다"}, seen))
    f = _flow(f"count=1&ofs=1&req0___data__={CAPTURED}")
    asyncio.run(CampfireAddon().request(f))
    assert f.response is None, "가려서 보내야 한다(차단 아님)"
    assert seen == [("CFPROBE 캡처 시험입니다", True, False)], "프롬프트 기준(인젝션 검사 없이)으로 한 번만 검사"
    body = f.request.get_text()
    assert body.startswith("count=1&ofs=1&"), "count·ofs 는 그대로"
    assert _prompt_of(body) == "[가림] 시험입니다"
    assert "CFPROBE" not in base64.b64decode(body.split("req0___data__=")[1].replace("%2B", "+").replace("%2F", "/")).decode()


def test_사용자_글이_없는_요청은_그대로_통과한다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    seen = []
    monkeypatch.setattr(orch, "run_pipeline", _fake({}, seen))
    for body in ("count=0", "count=1&ofs=2&req0___data__=IgIIAQ=="):  # 세션 열기, 턴 끝({4: {1: 1}})
        f = _flow(body)
        asyncio.run(CampfireAddon().request(f))
        assert f.response is None and f.request.get_text() == body
    assert seen == []


def test_base64_의_더하기가_날것으로_와도_읽는다(monkeypatch, decide_masked):
    # parse_qsl 로 풀면 '+' 가 공백이 돼 base64 가 깨진다. '+' 가 나오는 글을 골라 날것으로 싣는다.
    import app.core.pipeline.orchestrator as orch
    text = "메모 ~~~ 010-1234-5678"
    raw = base64.b64encode(_turn(text)).decode()
    assert "+" in raw, "이 글의 base64 에 '+' 가 있어야 이 시험이 뜻이 있다"
    seen = []
    monkeypatch.setattr(orch, "run_pipeline", _fake({text: "전화 [가림]"}, seen))
    f = _flow(f"count=1&ofs=1&req0___data__={quote(raw, safe='=+/')}")
    asyncio.run(CampfireAddon().request(f))
    assert seen and seen[0][0] == text
    assert _prompt_of(f.request.get_text()) == "전화 [가림]"


def test_여러_칸을_한_번에_검사하고_판단은_한_번(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    seen, asked = [], []

    async def wait(**_kw):
        asked.append(1)
        return "send_masked"

    monkeypatch.setattr(addon_mod.broker, "wait", wait)
    monkeypatch.setattr(orch, "run_pipeline", _fake({"첫 글 010-1111-2222": "첫 글 [가림]"}, seen))
    a = base64.b64encode(_turn("첫 글 010-1111-2222")).decode()
    b = base64.b64encode(_turn("둘째 글")).decode()
    f = _flow(f"count=2&ofs=1&req0___data__={quote(a, safe='=')}&req1___data__={quote(b, safe='=')}")
    asyncio.run(CampfireAddon().request(f))
    assert [s[0] for s in seen] == ["첫 글 010-1111-2222", "둘째 글"]
    assert asked == [1]
    assert _prompt_of(f.request.get_text()) == "첫 글 [가림]"


def test_못_읽는_데이터와_글_칸_없는_사용자_턴은_막는다():
    for data in ("!!!not-base64!!!", base64.b64encode(_ld(2, _ld(9, b"\x08\x01"))).decode()):
        f = _flow(f"count=1&ofs=1&req0___data__={quote(data, safe='=')}")
        asyncio.run(CampfireAddon().request(f))
        assert f.response is not None and f.response.status_code == 403, data


def test_전송_호스트가_PAC_와_복호화_목록에_있다():
    exact, _ = addon_mod.intercept_hosts()
    assert addon_mod.GEMINI_PROMPT_HOST in exact, "PAC 밖이면 크롬이 QUIC 로 직결해 프록시가 못 본다"


def test_답_롱폴은_흘려보낸다():
    f = tflow.tflow(req=tutils.treq(method="GET", host=addon_mod.GEMINI_PROMPT_HOST, path=PATH + "&TYPE=xmlhttp"),
                    resp=True)
    CampfireAddon().responseheaders(f)
    assert f.response.stream is True
    other = tflow.tflow(req=tutils.treq(method="GET", host="chatgpt.com", path="/x"), resp=True)
    CampfireAddon().responseheaders(other)
    assert not other.response.stream
