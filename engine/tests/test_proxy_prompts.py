"""Claude 밖 사이트의 프롬프트 전송 — 프록시가 검사·마스킹한다.

2026-10-22 실측(addon.py PROMPT_* 표 주석): ChatGPT(비로그인)는 form 의 prompt,
Perplexity 는 JSON 의 query_str·params.dsl_query 에 친 글을 싣는다. 자동완성·분석
요청도 친 글을 그대로 실어서 보내지 않는다.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qsl, urlencode

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow, tutils  # noqa: E402

from app.adapters.proxy.addon import CampfireAddon, intercept_hosts  # noqa: E402

PII = "주민번호 900101-1234567 확인해줘"
MASKED = "주민번호 [주민등록번호 마스킹] 확인해줘"


def _fake_pipeline(calls):
    async def fake(*, text=None, direct_prompt=False, **kw):
        calls.append((text, direct_prompt))
        m = text.replace("900101-1234567", "[주민등록번호 마스킹]")
        pii = [{"type": "RRN", "start": 0, "end": 1}] if m != text else []
        return {"maskedText": m, "piiItems": pii, "injectionItems": [], "blocked": False,
                "scanStatus": "ok", "truncated": False,
                "stats": {"piiCount": len(pii), "injectionCount": 0}}
    return fake


def _flow(host, path, body: str, ctype, method="POST"):
    f = tflow.tflow(req=tutils.treq(method=method, host=host, path=path, content=body.encode()))
    f.request.headers["content-type"] = ctype
    return f


async def _send(addon, f, action="send_masked"):
    """요청을 보내고, 검토가 뜨면 action 으로 답한다. 뜬 검토 수를 돌려준다."""
    from app.adapters.proxy.decision import broker
    task = asyncio.create_task(addon.request(f))
    await asyncio.sleep(0.05)
    pend = broker.list_pending()
    for p in pend:
        broker.resolve(p["id"], action)
    await task
    return len(pend)


CHATGPT = ("chatgpt.com", "/unauth-mweb/conversation/updates?lightweight_authenticated=0")
FORM = "application/x-www-form-urlencoded;charset=UTF-8"


def _chatgpt_body(prompt):
    return urlencode([("conversationState", '{"messages":[]}'), ("prompt", prompt),
                      ("timezone", "Asia/Seoul"), ("proofToken", "gAAAA+/=")])


def test_ChatGPT_프롬프트를_가려_보낸다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    calls = []
    monkeypatch.setattr(orch, "run_pipeline", _fake_pipeline(calls))
    f = _flow(*CHATGPT, _chatgpt_body(PII), FORM)
    assert asyncio.run(_send(CampfireAddon(), f)) == 1
    sent = dict(parse_qsl(f.request.get_text(), keep_blank_values=True))
    assert sent["prompt"] == MASKED
    # 나머지 칸은 그대로(토큰의 +/= 도 깨지지 않는다)
    assert sent["proofToken"] == "gAAAA+/=" and sent["timezone"] == "Asia/Seoul"
    assert calls == [(PII, True)], "프롬프트 기준(direct_prompt)으로 한 번만 검사"
    assert f.response is None


def test_ChatGPT_깨끗하면_본문을_건드리지_않는다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    monkeypatch.setattr(orch, "run_pipeline", _fake_pipeline([]))
    body = _chatgpt_body("오늘 날씨 어때")
    f = _flow(*CHATGPT, body, FORM)
    assert asyncio.run(_send(CampfireAddon(), f)) == 0
    assert f.request.get_text() == body and f.response is None


def test_ChatGPT_프롬프트_칸이_없으면_막는다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    monkeypatch.setattr(orch, "run_pipeline", _fake_pipeline([]))
    f = _flow(*CHATGPT, urlencode([("message", PII)]), FORM)
    asyncio.run(_send(CampfireAddon(), f))
    assert f.response is not None and f.response.status_code == 403


def test_ChatGPT_취소하면_막는다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    monkeypatch.setattr(orch, "run_pipeline", _fake_pipeline([]))
    f = _flow(*CHATGPT, _chatgpt_body(PII), FORM)
    asyncio.run(_send(CampfireAddon(), f, action="cancel"))
    assert f.response is not None and f.response.status_code == 403


PPLX = ("www.perplexity.ai", "/rest/sse/perplexity_ask")


def test_Perplexity_두_칸을_한_번에_검사해_같이_가린다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    calls = []
    monkeypatch.setattr(orch, "run_pipeline", _fake_pipeline(calls))
    body = {"params": {"language": "ko-KR", "dsl_query": PII, "time_from_first_type": 5092.300000011921},
            "query_str": PII}
    f = _flow(*PPLX, json.dumps(body, ensure_ascii=False), "application/json")
    assert asyncio.run(_send(CampfireAddon(), f)) == 1, "같은 글은 검토도 한 번"
    sent = json.loads(f.request.get_text())
    assert sent["query_str"] == MASKED and sent["params"]["dsl_query"] == MASKED
    assert sent["params"]["time_from_first_type"] == 5092.300000011921
    assert calls == [(PII, True)]


def test_Perplexity_해석_못하면_막는다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    monkeypatch.setattr(orch, "run_pipeline", _fake_pipeline([]))
    for raw in ("not json", json.dumps({"params": {"dsl_query": PII}})):
        f = _flow(*PPLX, raw, "application/json")
        asyncio.run(_send(CampfireAddon(), f))
        assert f.response is not None and f.response.status_code == 403, raw


@pytest.mark.parametrize("host,path,method", [
    ("grok.com", "/_worker/typeahead?lang=ko&q=%EC%A3%BC%EB%AF%BC", "GET"),
    ("suggest.perplexity.ai", "/suggest/ws", "GET"),
    ("suggest.perplexity.ai", "/search/v2/navigate", "POST"),
    ("www.perplexity.ai", "/rest/event/analytics", "POST"),
])
def test_친_글을_싣는_부수_요청은_보내지_않는다(host, path, method):
    f = _flow(host, path, json.dumps({"q": PII}), "application/json", method=method)
    asyncio.run(CampfireAddon().request(f))
    assert f.response is not None and f.response.status_code == 204


def test_새_호스트가_PAC_대상에_들어간다():
    exact, _ = intercept_hosts()
    assert {"suggest.perplexity.ai", "www.perplexity.ai", "grok.com", "chatgpt.com"} <= set(exact)
