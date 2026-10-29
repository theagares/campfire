"""Gemini 파일 — 확장처럼 등록부터 _masked.md 로 보이게 ①시작 이름 ②본문 ③ProcessFile ④전송 첨부 항목을 함께 바꾼다.

2026-10-29 맥 캡처(cfprobe.txt): ① "File name: cfprobe.txt" ② 응답 본문 = 파일 경로 ③ f.req 에 [경로, null, 1, MIME]·이름
④ processSession ofs=0 세션 설정 안 {1: {1: 경로, 2: 3, 4: MIME, 5: 파일 id}, 2: 이름, 7: 0}.
예전엔 ②만 가린 글로 바꿀 수 없어 가릴 것이 있으면 막았다(맥: docx·png·py 전부 막힘).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qsl, quote, unquote, urlencode

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy import http  # noqa: E402
from mitmproxy.test import tflow, tutils  # noqa: E402

import app.adapters.proxy.addon as addon_mod  # noqa: E402
from app.adapters.proxy import protobuf as pb  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon  # noqa: E402

UPLOAD = "push.clients6.google.com"
SESSION_URL = f"https://{UPLOAD}/upload/?upload_id=U1&upload_protocol=resumable"
PATH = "/contrib_service/ttl_1d/yqc2pdbavl3hnoea54exkpg24zn62p1793242050_ARqe1M"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _run(c):
    return asyncio.run(c)


def _start(a, name="보고서.docx", declared=1000):
    f = tflow.tflow(req=tutils.treq(method="POST", host=UPLOAD, path="/upload/",
                                    content=f"File name: {name}".encode()))
    f.request.headers["x-goog-upload-command"] = "start"
    f.request.headers["x-goog-upload-header-content-length"] = str(declared)
    _run(a.request(f))
    if f.response is None:
        f.response = http.Response.make(200, b"", {"x-goog-upload-url": SESSION_URL, "x-goog-upload-status": "active"})
        a.response(f)
    return f


def _finalize(a, content=b"PK\x03\x04 docx bytes", path=PATH):
    f = tflow.tflow(req=tutils.treq(method="POST", host=UPLOAD, path="/upload/?upload_id=U1&upload_protocol=resumable",
                                    content=content))
    f.request.headers["x-goog-upload-command"] = "upload, finalize"
    _run(a.request(f))
    if f.response is None:
        f.response = http.Response.make(200, path.encode(), {"x-goog-upload-status": "final"})
        a.response(f)
    return f


def _pipeline(monkeypatch, *, text="본문", pii=0, status="ok"):
    import app.core.pipeline.orchestrator as orch

    async def run(**_kw):
        items = [{"type": "PHONE", "start": 0, "end": 1}] * pii
        return {"originalText": text, "maskedText": "[가림] 본문" if pii else text, "piiItems": items,
                "injectionItems": [], "forcedMaskItems": [], "blocked": False, "scanStatus": status,
                "truncated": False, "stats": {"piiCount": pii, "injectionCount": 0, "forcedMaskCount": 0}}

    async def wait(**_kw):
        return "send_original"  # 원본을 골라도 _masked.md 로 등록한 자리라 가린 글이 가야 한다

    monkeypatch.setattr(orch, "run_pipeline", run)
    monkeypatch.setattr(addon_mod.broker, "wait", wait)


def test_시작_요청의_이름을_masked_md_로_바꾸고_길이를_키운다():
    a = CampfireAddon()
    f = _start(a)
    assert f.request.content.decode() == "File name: 보고서_masked.md"
    assert int(f.request.headers["x-goog-upload-header-content-length"]) > 1000


def test_시작_요청이_모르는_모양이면_막는다():
    a = CampfireAddon()
    f = tflow.tflow(req=tutils.treq(method="POST", host=UPLOAD, path="/upload/", content=b"x" * 20))
    f.request.headers["x-goog-upload-command"] = "start"
    f.request.headers["x-goog-upload-header-content-length"] = "20"
    _run(a.request(f))
    assert f.response is not None and f.response.status_code == 403


@pytest.mark.parametrize("pii, want", [(0, "본문"), (2, "[가림] 본문")])
def test_본문은_깨끗해도_원본이_아니라_글로_올리고_경로를_이름에_묶는다(monkeypatch, pii, want):
    _pipeline(monkeypatch, pii=pii)
    a = CampfireAddon()
    start = _start(a)
    nprime = int(start.request.headers["x-goog-upload-header-content-length"])
    f = _finalize(a)
    assert f.request.content.rstrip(b" ").decode() == want and len(f.request.content) == nprime
    assert b"PK" not in f.request.content, "원본(docx) 바이트를 _masked.md 자리에 올렸다"
    assert a._gemini_files == {PATH: "보고서_masked.md"}


def test_검사를_끝내지_못한_파일은_원본을_보내지_않는다(monkeypatch):
    # 이미지 검사가 꺼진 그림 등 — 확장 방식과 같이 원본 그림을 보내지 않는다.
    _pipeline(monkeypatch, status="unsupported")
    a = CampfireAddon()
    _start(a, name="사진.png")
    f = _finalize(a, content=b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    assert f.response is not None and f.response.status_code == 403
    assert a._gemini_files == {}


def test_확장이_검토한_마스킹본은_이름을_바꾸지_않고_그대로_보낸다(monkeypatch):
    monkeypatch.setattr(addon_mod, "_approved", {})
    data = "# 확장이 가린 글".encode()
    addon_mod.approve_uploads([(hashlib.sha256(data).hexdigest(), "보고서_masked.md", len(data))])
    a = CampfireAddon()
    start = _start(a, name="보고서_masked.md", declared=len(data))
    assert start.request.content.decode() == "File name: 보고서_masked.md", "x_masked_masked.md 로 두 번 바꿨다"
    f = _finalize(a, content=data)
    assert f.request.content.rstrip(b" ") == data
    assert a._gemini_files == {}, "이름을 안 바꾼 파일은 ③④ 에서 고칠 것이 없다"


def test_ProcessFile_의_형식과_이름을_맞춘다():
    a = CampfireAddon()
    a._gemini_files[PATH] = "보고서_masked.md"
    inner = [[[PATH, None, 1, DOCX], "보고서.docx"], None, 1, ["ko"]]
    body = urlencode([("f.req", json.dumps([None, json.dumps(inner)])), ("at", "TOKEN")])
    f = tflow.tflow(req=tutils.treq(method="POST", host="gemini.google.com",
                                    path=addon_mod.GEMINI_PROCESS_FILE_PATH + "?bl=x&rt=c", content=body.encode()))
    _run(a.request(f))
    pairs = dict(parse_qsl(f.request.get_text()))
    got = json.loads(json.loads(pairs["f.req"])[1])
    assert got[0] == [[PATH, None, 1, "text/plain"], "보고서_masked.md"]
    assert pairs["at"] == "TOKEN"


def test_모르는_파일의_ProcessFile_은_그대로_둔다():
    a = CampfireAddon()
    a._gemini_files[PATH] = "보고서_masked.md"
    inner = [[["/contrib_service/other", None, 1, DOCX], "남의.docx"], None, 1, ["ko"]]
    body = urlencode([("f.req", json.dumps([None, json.dumps(inner)])), ("at", "TOKEN")])
    f = tflow.tflow(req=tutils.treq(method="POST", host="gemini.google.com",
                                    path=addon_mod.GEMINI_PROCESS_FILE_PATH, content=body.encode()))
    _run(a.request(f))
    assert f.request.get_text() == body


def _ld(num, data):
    return pb._encode_varint((num << 3) | 2) + pb._encode_varint(len(data)) + data


def _vi(num, n):
    return pb._encode_varint(num << 3) + pb._encode_varint(n)


def test_전송_세션_설정의_첨부_항목_형식과_이름을_맞춘다(monkeypatch):
    import app.core.pipeline.orchestrator as orch
    seen = []

    async def run(**kw):
        seen.append(kw)

    monkeypatch.setattr(orch, "run_pipeline", run)
    a = CampfireAddon()
    a._gemini_files[PATH] = "보고서_masked.md"
    item = (_ld(1, _ld(1, PATH.encode()) + _vi(2, 3) + _ld(4, DOCX.encode()) + _ld(5, b"107569b7-file-id"))
            + _ld(2, "보고서.docx".encode()) + _vi(7, 0))
    config = _ld(1, b"ko") + _ld(8, _ld(5, item)) + _ld(9, b"!BotGuardToken")
    body = f"count=1&ofs=0&req0___data__={quote(base64.b64encode(config).decode(), safe='=')}"
    f = tflow.tflow(req=tutils.treq(method="POST", host=addon_mod.GEMINI_PROMPT_HOST,
                                    path="/v1/processSession?VER=8&SID=s&RID=2", content=body.encode()))
    _run(a.request(f))
    assert f.response is None and seen == [], "세션 설정은 검사할 글이 아니다"
    out = f.request.get_text()
    assert out.startswith("count=1&ofs=0&")
    msg = pb.decode(base64.b64decode(unquote(out.split("req0___data__=")[1])))
    got = {path: fld.value for path, fld in msg.walk() if fld.value is not None}
    assert got[(8, 5, 1, 4)] == b"text/plain"
    assert got[(8, 5, 2)] == "보고서_masked.md".encode()
    assert got[(8, 5, 1, 1)] == PATH.encode() and got[(8, 5, 1, 5)] == b"107569b7-file-id"
    assert got[(1,)] == b"ko" and got[(9,)] == b"!BotGuardToken", "다른 칸은 그대로"
