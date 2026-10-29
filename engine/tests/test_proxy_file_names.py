"""맥 한글 파일 이름(NFD) — 바꾼 이름은 NFC, 확장이 알린 이름과는 NFC 로 견주고, 등록어는 NFD 이름에서도 찾는다.

맥 v0.2.59: Gemini 시작 요청 본문이 "File name: 보고서.docx" 를 NFD(자모 분해) 날것 UTF-8 로 보냈다(len 34).
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow, tutils  # noqa: E402

import app.adapters.proxy.addon as addon_mod  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon  # noqa: E402

NFC = "보고서.docx"
NFD = unicodedata.normalize("NFD", NFC)


def test_바꾼_이름은_NFC_다():
    assert NFD != NFC
    assert addon_mod.masked_name_for(NFD) == "보고서_masked.md"
    assert addon_mod.masked_name_for(NFD) == unicodedata.normalize("NFC", addon_mod.masked_name_for(NFD))


def test_등록어는_NFD_이름에서도_찾는다():
    from app.core import forced_mask

    forced_mask.registry.configure(["보고서"])
    assert addon_mod.masked_name_for(NFD) == "document_masked.md"


def test_확장이_알린_NFC_이름을_NFD_로_와도_알아본다(monkeypatch):
    monkeypatch.setattr(addon_mod, "_approved", {})
    addon_mod.approve_uploads([(hashlib.sha256(b"x").hexdigest(), "보고서_masked.md", 10)])
    assert addon_mod._approved_meta(unicodedata.normalize("NFD", "보고서_masked.md"), 10)


def test_Gemini_시작_요청의_NFD_이름을_NFC_마스킹_이름으로_바꾼다():
    body = f"File name: {NFD}".encode()
    assert len(body) == 34, "맥에서 본 길이(자모 분해 날것 UTF-8)와 같아야 이 시험이 뜻이 있다"
    f = tflow.tflow(req=tutils.treq(method="POST", host="push.clients6.google.com", path="/upload/", content=body))
    f.request.headers["x-goog-upload-command"] = "start"
    f.request.headers["x-goog-upload-header-content-length"] = "100"
    asyncio.run(CampfireAddon().request(f))
    assert f.request.content.decode() == "File name: 보고서_masked.md"


def test_multipart_업로드의_NFD_파일_이름도_NFC_마스킹_이름으로(monkeypatch):
    import app.core.pipeline.orchestrator as orch

    async def run(**_kw):
        return {"originalText": "김민준", "maskedText": "[이름 마스킹]", "piiItems": [{"type": "PERSON", "start": 0, "end": 3}],
                "injectionItems": [], "forcedMaskItems": [], "blocked": False, "scanStatus": "ok", "truncated": False,
                "stats": {"piiCount": 1, "injectionCount": 0, "forcedMaskCount": 0}}

    async def wait(**_kw):
        return "send_masked"

    monkeypatch.setattr(orch, "run_pipeline", run)
    monkeypatch.setattr(addon_mod.broker, "wait", wait)
    boundary = "XyZ"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{NFD}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\nPK\r\n--{boundary}--\r\n").encode()
    f = tflow.tflow(req=tutils.treq(method="POST", host="claude.ai", path="/api/org/upload", content=body))
    f.request.headers["content-type"] = f"multipart/form-data; boundary={boundary}"
    asyncio.run(CampfireAddon().request(f))
    assert 'filename="보고서_masked.md"'.encode() in f.request.content
