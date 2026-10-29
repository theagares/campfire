"""ChatGPT 이미지 — 사용자 결정 1안: 그림은 그림으로 등록된 채 두고, PUT 에서 OCR 로 검사해 깨끗하면 원본 그대로,
가릴 것이 있거나 검사를 끝내지 못했으면 403 + 데스크톱 알림.

#307(결정 A, 늘 .md)을 되돌린다 — 그림을 _masked.md 문서로 등록·처리·전송하면 ChatGPT 가 보내기를 거부했다(맥 R-3:
클라이언트는 그림으로 다루는데 서버 응답은 text/markdown). 2026-10-29 맥 캡처(cfprobe.png): ① use_case "multimodal"·mime
image/png → file_id ② PUT ct=image/png ③ process_upload_stream ④ parts 의 image_asset_pointer + attachments.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy import http  # noqa: E402
from mitmproxy.test import tflow, tutils  # noqa: E402

import app.adapters.proxy.addon as addon_mod  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon  # noqa: E402

FID = "file_0000000076dc820997dc29d6d4e10227"
BLOB = "sdmntprjapaneast.oaiusercontent.com"
RAW = "/files/00000000-76dc/raw"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
REGISTER = {"entry_surface": "chat_composer", "file_name": "image.png", "file_size": 6840,
            "client_resolved_mime_type": "image/png", "mime_resolution_source": "filename_extension",
            "mime_type": "image/png", "use_case": "multimodal", "store_in_library": True}


def _run(c):
    return asyncio.run(c)


def _register(a, body=None):
    body = dict(REGISTER if body is None else body)
    f = tflow.tflow(req=tutils.treq(method="POST", host="chatgpt.com", path="/backend-api/files",
                                    content=json.dumps(body).encode()))
    f.request.headers["content-type"] = "application/json"
    _run(a.request(f))
    if f.response is None:
        f.response = http.Response.make(200, json.dumps(
            {"status": "success", "upload_url": f"https://{BLOB}{RAW}?sig=x", "file_id": FID}).encode(),
            {"content-type": "application/json"})
        a.response(f)
    return f


def _put(a, content=PNG):
    f = tflow.tflow(req=tutils.treq(method="PUT", host=BLOB, path=RAW + "?sig=x", content=content))
    f.request.headers["content-type"] = "image/png"
    _run(a.request(f))
    return f


@pytest.fixture
def scan(monkeypatch):
    """그림 OCR 결과를 흉내 낸다 — state 의 pii·status 로. 알림은 notices 로."""
    import app.core.pipeline.orchestrator as orch

    state = {"pii": 0, "status": "ok", "notices": [], "blocked": False}

    async def run(**kw):
        if kw.get("text") is not None:  # 전송 메시지의 프롬프트
            return {"maskedText": kw["text"], "piiItems": [], "injectionItems": [], "forcedMaskItems": [],
                    "blocked": False, "scanStatus": "ok", "truncated": False}
        items = [{"type": "PHONE", "start": 0, "end": 1}] * state["pii"]
        return {"originalText": "CFPROBE", "maskedText": "[가림]" if state["pii"] else "CFPROBE", "piiItems": items,
                "injectionItems": [], "forcedMaskItems": [], "blocked": state["blocked"], "scanStatus": state["status"],
                "truncated": False, "stats": {"piiCount": state["pii"], "injectionCount": 0, "forcedMaskCount": 0}}

    async def wait(**_kw):
        raise AssertionError("그림은 묻지 않는다 — 깨끗하면 원본, 아니면 막는다")

    monkeypatch.setattr(orch, "run_pipeline", run)
    monkeypatch.setattr(addon_mod.broker, "wait", wait)
    monkeypatch.setattr(addon_mod.broker, "notify", lambda host, reason: state["notices"].append(reason))
    return state


def test_그림_등록은_그대로_둔다(scan):
    f = _register(CampfireAddon())
    assert json.loads(f.request.get_text()) == REGISTER, "깨끗하면 원본을 보내야 해서 이름·형식·크기를 바꾸면 안 된다"


def test_문서_등록은_지금처럼_masked_md_로_바꾼다(scan):
    doc = dict(REGISTER, file_name="보고서.pdf", mime_type="application/pdf", client_resolved_mime_type="application/pdf",
               use_case="my_files")
    sent = json.loads(_register(CampfireAddon(), doc).request.get_text())
    assert sent["file_name"] == "보고서_masked.md" and sent["mime_type"] == "text/markdown" and sent["file_size"] > 6840


def test_깨끗한_그림은_원본_그대로_올린다(scan):
    a = CampfireAddon()
    _register(a)
    f = _put(a)
    assert f.response is None
    assert f.request.content == PNG and f.request.headers["content-type"] == "image/png"


def test_가릴_것이_있는_그림은_막고_알린다(scan):
    scan["pii"] = 2
    a = CampfireAddon()
    _register(a)
    f = _put(a)
    assert f.response is not None and f.response.status_code == 403
    assert json.loads(f.response.content)["reason"] == addon_mod._IMAGE_HAS_PII
    assert scan["notices"] == [addon_mod._IMAGE_HAS_PII]
    again = _put(a)  # 사이트의 재시도도 막는다
    assert again.response is not None and again.response.status_code == 403


def test_검사를_끝내지_못한_그림은_보내지_않고_이유를_알린다(scan):
    scan["status"] = "unsupported"  # 이미지 검사가 꺼진 경우
    a = CampfireAddon()
    _register(a)
    f = _put(a)
    assert f.response is not None and f.response.status_code == 403
    assert scan["notices"] == [addon_mod._IMAGE_UNSCANNED]


def test_사용자_지정_단어가_켜져_정책으로_막힌_그림도_알린다(scan):
    # 맥 v0.2.60 M-7: 이미지 검사 꺼짐 + 사용자 지정 단어 켜짐 — 파이프라인이 검사 못 한 그림을 blocked 로 돌려줘
    # "정책 차단" 으로 막혔는데, 사람이 취소한 것(_CANCELLED)처럼 다뤄 운영체제 알림이 없었다.
    scan["status"], scan["blocked"] = "unsupported", True
    a = CampfireAddon()
    _register(a)
    f = _put(a)
    assert f.response is not None and f.response.status_code == 403
    assert json.loads(f.response.content)["reason"] == addon_mod._IMAGE_UNSCANNED
    assert scan["notices"] == [addon_mod._IMAGE_UNSCANNED]


def test_이름에_등록어가_있는_그림은_등록에서_막는다(scan, monkeypatch):
    from app.core import forced_mask

    forced_mask.registry.configure(["오로라"])
    try:
        f = _register(CampfireAddon(), dict(REGISTER, file_name="오로라_설계.png"))
    finally:
        forced_mask.registry.configure([])
    assert f.response is not None and f.response.status_code == 403
    assert scan["notices"] == [addon_mod._IMAGE_HAS_PII]


def test_전송_메시지의_그림_포인터는_건드리지_않는다(scan):
    body = {"action": "next", "messages": [{"author": {"role": "user"}, "content": {
        "content_type": "multimodal_text",
        "parts": [{"content_type": "image_asset_pointer", "asset_pointer": f"sediment://{FID}"}, "이 그림 설명해 줘"]},
        "metadata": {"attachments": [{"id": FID, "mime_type": "image/png", "name": "image.png", "width": 240}]}}]}
    f = tflow.tflow(req=tutils.treq(method="POST", host="chatgpt.com", path="/backend-api/f/conversation",
                                    content=json.dumps(body, ensure_ascii=False).encode()))
    f.request.headers["content-type"] = "application/json"
    a = CampfireAddon()
    _register(a)
    _put(a)
    _run(a.request(f))
    assert f.response is None and json.loads(f.request.get_text()) == body
