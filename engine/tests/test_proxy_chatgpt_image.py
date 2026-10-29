"""ChatGPT 이미지 — 사용자 결정 A: 그림은 늘 OCR 글(.md)로. ①등록 ②PUT ③process_upload_stream ④전송을 file_id 로 이어 바꾼다.

2026-10-29 맥 캡처(cfprobe.png 240x80): ① use_case "multimodal"·mime image/png → file_id ② PUT ct=image/png
③ process_upload_stream {file_id, file_name, mime_type, use_case} ④ parts 의 image_asset_pointer + attachments.
예전엔 ②만 바꿀 수 없어 가릴 것이 있으면 막았다(맥: 칩 "image.png 업로드 실패", 전송 버튼 꺼짐, 안내 없음).
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


def _run(c):
    return asyncio.run(c)


def _register(a, *, image=True):
    body = {"entry_surface": "chat_composer", "file_name": "image.png" if image else "보고서.pdf", "file_size": 6840,
            "client_resolved_mime_type": "image/png" if image else "application/pdf",
            "mime_resolution_source": "filename_extension", "mime_type": "image/png" if image else "application/pdf",
            "use_case": "multimodal" if image else "my_files", "store_in_library": True}
    f = tflow.tflow(req=tutils.treq(method="POST", host="chatgpt.com", path="/backend-api/files",
                                    content=json.dumps(body).encode()))
    f.request.headers["content-type"] = "application/json"
    _run(a.request(f))
    f.response = http.Response.make(200, json.dumps(
        {"status": "success", "upload_url": f"https://{BLOB}{RAW}?sig=x", "file_id": FID}).encode(),
        {"content-type": "application/json"})
    a.response(f)
    return json.loads(f.request.get_text())


def _put(a, content=PNG):
    f = tflow.tflow(req=tutils.treq(method="PUT", host=BLOB, path=RAW + "?sig=x", content=content))
    f.request.headers["content-type"] = "image/png"
    _run(a.request(f))
    return f


def _pipeline(monkeypatch, *, text="CFPROBE", pii=0, status="ok"):
    import app.core.pipeline.orchestrator as orch
    seen = []

    async def run(**kw):
        seen.append(kw)
        if kw.get("text") is not None:  # 전송 메시지의 프롬프트
            return {"maskedText": kw["text"], "piiItems": [], "injectionItems": [], "forcedMaskItems": [],
                    "blocked": False, "scanStatus": "ok", "truncated": False}
        items = [{"type": "PHONE", "start": 0, "end": 1}] * pii
        return {"originalText": text, "maskedText": "[가림]" if pii else text, "piiItems": items, "injectionItems": [],
                "forcedMaskItems": [], "blocked": False, "scanStatus": status, "truncated": False,
                "stats": {"piiCount": pii, "injectionCount": 0, "forcedMaskCount": 0}}

    async def wait(**_kw):
        return "send_original"  # 원본을 골라도 _masked.md 로 등록한 자리라 글이 가야 한다

    monkeypatch.setattr(orch, "run_pipeline", run)
    monkeypatch.setattr(addon_mod.broker, "wait", wait)
    return seen


def test_그림_등록을_masked_md_문서로_바꾸고_file_id_를_묶는다():
    a = CampfireAddon()
    body = _register(a)
    assert body["file_name"] == "image_masked.md"
    assert body["mime_type"] == body["client_resolved_mime_type"] == "text/markdown"
    assert body["use_case"] == "my_files", "그림 처리(multimodal)로 두면 MD 를 그림으로 읽다 거부한다"
    assert body["file_size"] > 6840
    assert a._chatgpt_files == {FID: "image_masked.md"}


def test_문서_등록은_use_case_를_건드리지_않는다():
    a = CampfireAddon()
    body = _register(a, image=False)
    assert body["use_case"] == "my_files" and body["file_name"] == "보고서_masked.md"
    assert a._chatgpt_files == {}, "문서는 ③④ 에서 고칠 것이 없다"


@pytest.mark.parametrize("pii, want", [(0, b"CFPROBE"), (2, b"[\xea\xb0\x80\xeb\xa6\xbc]")])
def test_그림은_깨끗해도_OCR_글로_올린다(monkeypatch, pii, want):
    _pipeline(monkeypatch, pii=pii)
    a = CampfireAddon()
    nprime = _register(a)["file_size"]
    f = _put(a)
    assert f.response is None
    assert f.request.content.rstrip(b" ") == want and len(f.request.content) == nprime
    assert f.request.headers["content-type"] == "text/markdown"


def test_검사를_끝내지_못한_그림은_보내지_않고_이유를_알린다(monkeypatch):
    # 이미지 검사가 꺼진 경우 — 확장 방식과 같이 검사 못 한 그림은 원본으로 보내지 않는다.
    _pipeline(monkeypatch, status="unsupported")
    a = CampfireAddon()
    _register(a)
    f = _put(a)
    assert f.response is not None and f.response.status_code == 403
    assert "검사를 끝내지 못한 그림".encode() in f.response.content


def test_process_upload_stream_을_등록과_맞춘다():
    a = CampfireAddon()
    a._chatgpt_files[FID] = "image_masked.md"
    body = {"entry_surface": "chat_composer", "file_id": FID, "file_name": "image.png", "index_for_retrieval": False,
            "mime_type": "image/png", "use_case": "multimodal"}
    f = tflow.tflow(req=tutils.treq(method="POST", host="chatgpt.com", path=addon_mod.CHATGPT_PROCESS_PATH,
                                    content=json.dumps(body).encode()))
    _run(a.request(f))
    got = json.loads(f.request.get_text())
    assert (got["file_name"], got["mime_type"], got["use_case"]) == ("image_masked.md", "text/markdown", "my_files")
    assert got["index_for_retrieval"] is False


def _conversation(parts, attachments):
    return {"action": "next", "messages": [{"author": {"role": "user"},
                                            "content": {"content_type": "multimodal_text", "parts": parts},
                                            "metadata": {"attachments": attachments}}]}


def test_전송_메시지의_그림_포인터를_첨부_문서로_바꾼다(monkeypatch):
    seen = _pipeline(monkeypatch)
    a = CampfireAddon()
    a._chatgpt_files[FID] = "image_masked.md"
    ptr = {"asset_pointer": f"sediment://{FID}", "content_type": "image_asset_pointer", "height": 80,
           "size_bytes": 6840, "width": 240}
    att = {"height": 80, "id": FID, "library_file_id": "libfile_x", "mime_type": "image/png",
           "name": "image(20261029-024932).png", "size": 6840, "width": 240}
    f = tflow.tflow(req=tutils.treq(method="POST", host="chatgpt.com", path="/backend-api/f/conversation",
                                    content=json.dumps(_conversation([ptr, "CFPROBE 그림"], [att])).encode()))
    _run(a.request(f))
    msg = json.loads(f.request.get_text())["messages"][0]
    assert msg["content"] == {"content_type": "text", "parts": ["CFPROBE 그림"]}
    assert msg["metadata"]["attachments"] == [{"id": FID, "library_file_id": "libfile_x", "mime_type": "text/markdown",
                                               "name": "image_masked.md", "size": 6840}]
    assert [kw.get("text") for kw in seen] == ["CFPROBE 그림"], "프롬프트는 그대로 검사한다"


def test_그림만_보낸_메시지도_글_없는_첨부_메시지가_된다(monkeypatch):
    _pipeline(monkeypatch)
    a = CampfireAddon()
    a._chatgpt_files[FID] = "image_masked.md"
    ptr = {"asset_pointer": f"sediment://{FID}", "content_type": "image_asset_pointer"}
    f = tflow.tflow(req=tutils.treq(method="POST", host="chatgpt.com", path="/backend-api/f/conversation",
                                    content=json.dumps(_conversation([ptr], [{"id": FID, "mime_type": "image/png"}])).encode()))
    _run(a.request(f))
    msg = json.loads(f.request.get_text())["messages"][0]
    assert msg["content"] == {"content_type": "text", "parts": [""]}


def test_모르는_그림은_그대로_둔다(monkeypatch):
    _pipeline(monkeypatch)
    a = CampfireAddon()
    a._chatgpt_files[FID] = "image_masked.md"
    ptr = {"asset_pointer": "sediment://file_other", "content_type": "image_asset_pointer"}
    f = tflow.tflow(req=tutils.treq(method="POST", host="chatgpt.com", path="/backend-api/f/conversation",
                                    content=json.dumps(_conversation([ptr, "질문"], [])).encode()))
    _run(a.request(f))
    assert json.loads(f.request.get_text())["messages"][0]["content"]["parts"][0] == ptr
