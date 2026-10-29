"""가린 뒤 다시 싼 본문에 원문 값이 남으면 보내지 않는다 — 다섯 사이트 공통 안전장치.

Windows v0.2.59 WPX-1(프록시 켬·확장 켬): Perplexity 에 "담당자: 김민준 / 연락처: … / 이메일: [x](mailto:x)" 를 보내자 모델은
가린 글을 받았는데 저장된 사용자 말풍선은 원문이었다. 같은 글이 검사한 칸 말고 다른 칸에도 실렸거나, 탐지가 mailto 괄호
안 두 번째 이메일을 놓친 경우다. 찾은 값은 같은 글 안에서 한 번 더 나와도 가리고, 그래도 본문 어디에(JSON·form·
base64 protobuf) 남아 있으면 403 으로 막고 알린다.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from urllib.parse import quote, urlencode

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow, tutils  # noqa: E402

import app.adapters.proxy.addon as addon_mod  # noqa: E402
from app.adapters.proxy import protobuf as pb  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon  # noqa: E402

NAME, PHONE, MAIL = "김민준", "010-2741-8853", "minjun.kim@example.com"
PROMPT = f"담당자: {NAME} / 연락처: {PHONE} / 이메일: [{MAIL}](mailto:{MAIL})"  # 맥 v0.2.59 perplexity_ask 캡처와 같은 모양
MASKED = "담당자: [이름 마스킹] / 연락처: [전화번호 마스킹] / 이메일: [[이메일 마스킹]](mailto:[이메일 마스킹])"


@pytest.fixture
def fake(monkeypatch):
    """값마다 첫 자리만 잡는 탐지(모델이 mailto 안쪽 두 번째 이메일을 놓친 경우). 판단은 action, 알림은 notices 로."""
    import app.core.pipeline.orchestrator as orch

    state = {"action": "send_masked", "notices": []}

    async def run(*, text=None, **_kw):
        items, masked = [], text
        for typ, ph, raw in (("PERSON_NAME", "[이름 마스킹]", NAME), ("PHONE", "[전화번호 마스킹]", PHONE),
                             ("EMAIL", "[이메일 마스킹]", MAIL)):
            i = text.find(raw)
            if i >= 0:
                items.append({"type": typ, "start": i, "end": i + len(raw), "text": raw, "confidence": 0.9})
                masked = masked.replace(raw, ph, 1)
        return {"originalText": text, "maskedText": masked, "piiItems": items, "injectionItems": [],
                "forcedMaskItems": [], "blocked": False, "scanStatus": "ok", "truncated": False,
                "stats": {"piiCount": len(items), "injectionCount": 0, "forcedMaskCount": 0}}

    async def wait(**_kw):
        return state["action"]

    monkeypatch.setattr(orch, "run_pipeline", run)
    monkeypatch.setattr(addon_mod.broker, "wait", wait)
    monkeypatch.setattr(addon_mod.broker, "notify", lambda host, reason: state["notices"].append(reason))
    return state


def _post(host, path, body: str, ctype):
    f = tflow.tflow(req=tutils.treq(method="POST", host=host, path=path, content=body.encode()))
    f.request.headers["content-type"] = ctype
    return f


def _pplx(**extra_params):
    params = {"attachments": [], "language": "ko-KR", "dsl_query": PROMPT, "time_from_first_type": 3597.4,
              **extra_params}
    return _post("www.perplexity.ai", "/rest/sse/perplexity_ask",
                 json.dumps({"params": params, "query_str": PROMPT}, ensure_ascii=False), "application/json")


def _blocked(f, fake):
    return (f.response is not None and f.response.status_code == 403
            and json.loads(f.response.content)["reason"] == addon_mod._RAW_LEFT and fake["notices"] == [addon_mod._RAW_LEFT])


def test_Perplexity_같은_글이_실린_칸을_모두_가리고_mailto_안쪽도_가린다(fake):
    f = _pplx(saved_query=PROMPT.replace(" / ", "  /  "))  # 모르는 칸 — 공백만 다른 같은 글
    asyncio.run(CampfireAddon().request(f))
    assert f.response is None, "가린 글로 보내야 한다"
    sent = json.loads(f.request.get_text())
    assert sent["query_str"] == MASKED and sent["params"]["dsl_query"] == MASKED
    assert sent["params"]["saved_query"] == MASKED.replace(" / ", "  /  ")
    assert sent["params"]["time_from_first_type"] == 3597.4


def test_Perplexity_다른_칸에_원문_값이_남으면_막고_알린다(fake):
    f = _pplx(title=f"{NAME} 님 문의")
    asyncio.run(CampfireAddon().request(f))
    assert _blocked(f, fake)


def test_원본_보내기를_고르면_막지_않는다(fake):
    fake["action"] = "send_original"
    f = _pplx(title=f"{NAME} 님 문의")
    before = json.loads(f.request.content)
    asyncio.run(CampfireAddon().request(f))
    assert f.response is None and json.loads(f.request.content) == before


def test_ChatGPT_비로그인_form_의_다른_칸에_남은_이메일도_찾는다(fake):
    body = urlencode([("conversationState", json.dumps({"messages": [{"memo": MAIL}]})), ("prompt", PROMPT)])
    assert MAIL not in body, "form 은 @ 를 %40 으로 싼다 — 날것만 보면 놓친다"
    f = _post("chatgpt.com", "/unauth-mweb/conversation/updates", body, "application/x-www-form-urlencoded")
    asyncio.run(CampfireAddon().request(f))
    assert _blocked(f, fake)


def _ld(num, data):
    return pb._encode_varint((num << 3) | 2) + pb._encode_varint(len(data)) + data


def test_Gemini_다른_데이터_칸에_원문_값이_남으면_막는다(fake):
    config = _ld(1, b"ko") + _ld(3, f"{NAME} 메모".encode())  # base64 protobuf 안의 모르는 칸
    turn = _ld(2, pb._encode_varint(1 << 3) + pb._encode_varint(1) + _ld(9, _ld(1, PROMPT.encode())))
    b64 = [quote(base64.b64encode(raw).decode(), safe="=") for raw in (config, turn)]
    f = _post(addon_mod.GEMINI_PROMPT_HOST, "/v1/processSession?VER=8&RID=3",
              f"count=2&ofs=0&req0___data__={b64[0]}&req1___data__={b64[1]}", "application/x-www-form-urlencoded")
    asyncio.run(CampfireAddon().request(f))
    assert _blocked(f, fake)


def test_Claude_승인한_첨부는_빼고_모르는_칸은_막는다(fake, monkeypatch):
    path = addon_mod.CLAUDE_MESSAGE_RPC_PATHS[0]
    doc = f"승인한 문서 — 담당 {NAME}".encode()
    monkeypatch.setattr(addon_mod, "_approved", {})
    addon_mod.approve_uploads([(hashlib.sha256(doc).hexdigest(), "doc_masked.md", len(doc))])
    ok = tflow.tflow(req=tutils.treq(method="POST", host="claude.ai", path=path,
                                     content=_ld(2, _ld(3, PROMPT.encode()) + _ld(15, _ld(4, doc)))))
    ok.request.headers["content-type"] = "application/proto"
    asyncio.run(CampfireAddon().request(ok))
    assert ok.response is None, "확장이 승인한 첨부의 같은 이름 때문에 막았다"
    assert NAME.encode() not in pb.decode(ok.request.content).fields[0].message.fields[0].value

    bad = tflow.tflow(req=tutils.treq(method="POST", host="claude.ai", path=path,
                                      content=_ld(2, _ld(3, PROMPT.encode()) + _ld(7, NAME.encode()))))
    bad.request.headers["content-type"] = "application/proto"
    asyncio.run(CampfireAddon().request(bad))
    assert _blocked(bad, fake)


def test_Grok_웹소켓도_원문_값이_남으면_버린다(fake):
    from mitmproxy import websocket
    from wsproto.frame_protocol import Opcode

    item = {"x_grok": {"input_chunks": [{"text": {"text": PROMPT}}]}, "memo": PHONE}
    f = tflow.twebsocketflow()
    f.request.host, f.request.path = "grok.com", "/ws/mgw/?uid=1"
    msg = json.dumps({"event": {"type": "response.create", "item": item}}, ensure_ascii=False)
    f.websocket.messages = [websocket.WebSocketMessage(Opcode.TEXT, True, msg.encode())]
    asyncio.run(CampfireAddon().websocket_message(f))
    assert f.websocket.messages[-1].dropped and fake["notices"] == [addon_mod._RAW_LEFT]


# ── 사이트별 짧은 기억 — 다른 요청으로 새는 것 ─────────────────────────────────
# Windows v0.2.59 WP-1: Gemini processSession 프롬프트는 잡아 가렸는데(pii=3) 저장된 사용자 메시지는 원문이었다(맥은 가려짐).
# 같은 글이 다른 요청으로도 나간 것으로 보여, 가려 보낸 원문 값을 그 사이트에 대해 2분 기억하고 그 사이 같은 사이트로
# 나가는 모든 요청(batchexecute 포함)에서 보이면 막는다.

def _gemini_prompt(text=PROMPT):
    turn = _ld(2, pb._encode_varint(1 << 3) + pb._encode_varint(1) + _ld(9, _ld(1, text.encode())))
    return _post(addon_mod.GEMINI_PROMPT_HOST, "/v1/processSession?VER=8&RID=3",
                 f"count=1&ofs=1&req0___data__={quote(base64.b64encode(turn).decode(), safe='=')}",
                 "application/x-www-form-urlencoded")


def _batchexecute(text):
    return _post("gemini.google.com", "/_/BardChatUi/data/batchexecute?rpcids=a1",
                 urlencode([("f.req", json.dumps([[["a1", json.dumps([text]), None, "generic"]]]))]),
                 "application/x-www-form-urlencoded;charset=UTF-8")


def _remembered_blocked(f, fake):
    return (f.response is not None and f.response.status_code == 403
            and json.loads(f.response.content)["reason"] == addon_mod._RAW_ELSEWHERE
            and addon_mod._RAW_ELSEWHERE in fake["notices"])


def test_Gemini_가려_보낸_값이_batchexecute_로_나가면_막는다(fake):
    a = CampfireAddon()
    p = _gemini_prompt()
    asyncio.run(a.request(p))
    assert p.response is None
    leak = _batchexecute(f"제목: {NAME} 연락처")
    asyncio.run(a.request(leak))
    assert _remembered_blocked(leak, fake)
    ok = _batchexecute("제목: 새 대화")
    asyncio.run(a.request(ok))
    assert ok.response is None, "원문 값이 없는 요청까지 막았다"


def test_검사_중에_같은_사이트로_동시에_나간_요청도_기다렸다가_막는다(fake, monkeypatch):
    async def go():
        release = asyncio.Event()

        async def wait(**_kw):
            await release.wait()
            return "send_masked"

        monkeypatch.setattr(addon_mod.broker, "wait", wait)
        a = CampfireAddon()
        p, leak = _gemini_prompt(), _batchexecute(f"제목: {NAME}")
        t1 = asyncio.create_task(a.request(p))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(a.request(leak))
        await asyncio.sleep(0.05)
        assert not t2.done(), "검사 중인데 같은 사이트의 다른 요청을 먼저 보냈다"
        release.set()
        await asyncio.gather(t1, t2)
        return p, leak

    p, leak = asyncio.run(go())
    assert p.response is None and _remembered_blocked(leak, fake)


def test_원본_보내기를_고른_값은_잊는다(fake):
    a = CampfireAddon()
    asyncio.run(a.request(_gemini_prompt()))
    fake["action"] = "send_original"
    again = _gemini_prompt(f"{NAME} 님께 보낼 메일")
    asyncio.run(a.request(again))
    after = _batchexecute(f"제목: {NAME}")
    asyncio.run(a.request(after))
    assert again.response is None and after.response is None


def test_기억은_시간이_지나면_잊는다(fake, monkeypatch):
    monkeypatch.setattr(addon_mod, "_SITE_MEMORY_S", -1.0)  # 기억하자마자 지난 것으로
    a = CampfireAddon()
    asyncio.run(a.request(_gemini_prompt()))
    later = _batchexecute(f"제목: {NAME}")
    asyncio.run(a.request(later))
    assert later.response is None


def test_다른_사이트의_요청은_기억과_상관없다(fake):
    a = CampfireAddon()
    asyncio.run(a.request(_gemini_prompt()))
    other = _post("www.perplexity.ai", "/rest/thread/save", json.dumps({"title": NAME}, ensure_ascii=False),
                  "application/json")
    asyncio.run(a.request(other))
    assert other.response is None


def test_Grok_웹소켓의_다른_메시지에_실려도_버린다(fake):
    from mitmproxy import websocket
    from wsproto.frame_protocol import Opcode

    f = tflow.twebsocketflow()
    f.request.host, f.request.path = "grok.com", "/ws/mgw/?uid=1"
    f.websocket.messages = []
    a = CampfireAddon()
    for event in ({"type": "response.create", "item": {"x_grok": {"input_chunks": [{"text": {"text": PROMPT}}]}}},
                  {"type": "conversation.title", "title": f"{NAME} 연락처"}):
        f.websocket.messages.append(websocket.WebSocketMessage(
            Opcode.TEXT, True, json.dumps({"event": event}, ensure_ascii=False).encode()))
        asyncio.run(a.websocket_message(f))
    first, second = f.websocket.messages
    assert not first.dropped and NAME not in first.text
    assert second.dropped and addon_mod._RAW_ELSEWHERE in fake["notices"]
