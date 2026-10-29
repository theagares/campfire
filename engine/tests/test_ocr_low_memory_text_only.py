"""PC 메모리가 모자라 이미지 검사 모델을 쓰지 못하면 문서 속 그림만 빼고 글은 가려 보낸다(사용자 결정 (가)).

이미지 검사 끔과 같은 길(orchestrator imagesRemoved → 확장·프록시가 원본 대신 글 .md)을 탄다 — 그림이 나가지 않으니 그림 속 PII
도 나가지 않는다. 예전(#342)엔 그 그림을 미검사로 두어 문서가 통째로 막혔다(이 PC: 남은 커밋 약 8.9GB < 띄우기 기준 10GB).
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from app.core.detectors import image_ocr
from app.core.parser import images as image_marks


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 120), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _empty_ocr_cache():
    image_ocr._cache.clear()
    yield
    image_ocr._cache.clear()


def test_메모리가_모자라_읽지_않은_그림은_빼고_미검사로_막지_않는다(monkeypatch):
    ocr = image_ocr.get()

    async def read(_blob):
        ocr.last_failure = image_ocr.LOW_MEMORY_REASON
        return None

    monkeypatch.setattr(ocr, "read", read)
    images: list[bytes] = []
    text = f"앞 글\n{image_marks.mark(images, _png())}\n뒤 글"
    out, infos = asyncio.run(image_ocr.fill(text, images))
    assert infos[0]["status"] == "removed" and infos[0]["reason"] == image_ocr.LOW_MEMORY_REASON
    assert out == "앞 글\n\n뒤 글", "그림 자리는 빠지고 글은 그대로 남아야 한다"


def test_다른_이유로_못_읽은_그림은_지금처럼_미검사(monkeypatch):
    ocr = image_ocr.get()

    async def read(_blob):
        ocr.last_failure = image_ocr.READ_FAILED_REASON
        return None

    monkeypatch.setattr(ocr, "read", read)
    images: list[bytes] = []
    _out, infos = asyncio.run(image_ocr.fill(image_marks.mark(images, _png()), images))
    assert infos[0]["status"] == "unchecked"


def test_프록시는_메모리_부족으로_뺀_그림을_알리고_원본_대신_글을_보낸다(monkeypatch):
    pytest.importorskip("mitmproxy")
    import app.adapters.proxy.addon as addon_mod
    import app.core.pipeline.orchestrator as orch

    async def run(**_kw):
        return {"originalText": "보고서 본문", "maskedText": "보고서 본문", "piiItems": [], "injectionItems": [],
                "forcedMaskItems": [], "blocked": False, "scanStatus": "ok", "truncated": False,
                "imagesRemoved": 2, "imagesRemovedReason": "memory"}

    notices = []
    monkeypatch.setattr(orch, "run_pipeline", run)
    monkeypatch.setattr(addon_mod.broker, "notify", lambda host, reason: notices.append(reason))
    got = asyncio.run(addon_mod.CampfireAddon()._scan_upload(
        data=b"PK docx", file_name="report.docx", mime="application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document", host="claude.ai"))
    assert got == "보고서 본문".encode(), "그림째 원본이 아니라 그림을 뺀 글을 보내야 한다"
    assert notices == ["PC 메모리가 부족해 문서 속 그림 2장은 빼고 글만 보냈습니다"]
