"""Gemini 그림 첨부 — 맥 v0.2.60 M-1: 그림을 가린 글(.md)로 바꿔 올렸는데 Gemini 가 그 글을 그림으로 읽어 엉터리 답을 냈다.

맥 캡처(DevTools, 프록시가 바꾸기 전): 세션 설정(ofs=0) 첨부 항목 (1,8,5,1,1,4) = {1: {1: 경로, 2: 종류, 4: MIME, 5: 파일 id},
2: 이름} 에서 종류가 png=1·docx=10 이고 나머지 칸 구조는 같다. 프록시는 MIME·이름만 바꾸고 종류(1=그림)는 남겼다.
문서(10)+text/plain+가린 글은 docx 에서 정상 요약으로 확인한 조합이라 그림도 10 으로 바꾼다.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from urllib.parse import quote, unquote

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow, tutils  # noqa: E402

import app.adapters.proxy.addon as addon_mod  # noqa: E402
from app.adapters.proxy import protobuf as pb  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon  # noqa: E402

PATH = "/contrib_service/ttl_1d/imagepathaaaaaaaaaaaaaaaa"
NAME = "김민준_사진.png"
FILE_ID = b"139caf8e-7696-4828-a4c1-b6589db41594"
DOCX = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ITEM = (1, 8, 5, 1, 1, 4)  # 캡처의 첨부 항목 자리


def _ld(num, data):
    return pb._encode_varint((num << 3) | 2) + pb._encode_varint(len(data)) + data


def _vi(num, n):
    return pb._encode_varint(num << 3) + pb._encode_varint(n)


def _doc_style_config():
    """문서 캡처 모양의 세션 설정 — {8: {5: {1: {1: 경로, 2: 3, 4: MIME, 5: id}, 2: 이름, 7: 0}}}."""
    item = (_ld(1, _ld(1, PATH.encode()) + _vi(2, 3) + _ld(4, b"image/png") + _ld(5, b"file-id"))
            + _ld(2, NAME.encode()) + _vi(7, 0))
    return _ld(1, b"ko") + _ld(8, _ld(5, item))


def test_칸_구조는_숫자와_MIME_만_남기고_글은_길이로():
    shape = addon_mod._pb_shape(pb.decode(_doc_style_config()))
    assert "8.5.1.2=3" in shape and "8.5.1.4=image/png" in shape and "8.5.7=0" in shape
    assert f"8.5.2=len{len(NAME.encode())}" in shape
    assert not any("김민준" in s or "contrib_service" in s for s in shape), "이름·경로가 로그에 남는다"


def test_ProcessFile_구조도_MIME_만_남긴다():
    shape = addon_mod._json_shape([[[PATH, None, 1, "image/png"], NAME], None, 1, ["ko"]])
    assert shape == [[[f"s{len(PATH)}", None, 1, "image/png"], f"s{len(NAME)}"], None, 1, ["s2"]]


def test_바꾼_첨부가_실린_세션_데이터는_칸_구조를_로그로_남긴다(caplog):
    a = CampfireAddon()
    a._gemini_files[PATH] = "김민준_사진_masked.md"
    body = f"count=1&ofs=0&req0___data__={quote(base64.b64encode(_doc_style_config()).decode(), safe='=')}"
    f = tflow.tflow(req=tutils.treq(method="POST", host=addon_mod.GEMINI_PROMPT_HOST,
                                    path="/v1/processSession?VER=8&SID=s&RID=2", content=body.encode()))
    with caplog.at_level(logging.INFO, logger="securedoc.proxy"):
        asyncio.run(a.request(f))
    lines = [r.getMessage() for r in caplog.records if "Gemini 세션 데이터 칸" in r.getMessage()]
    assert lines and "8.5.1.2=3" in lines[0] and "image/png" in lines[0]
    assert "김민준" not in lines[0]


def _captured_config(kind, mime, name, path=PATH):
    """맥 캡처 모양의 세션 설정 — 첨부 항목은 (1,8,5,1,1,4), 그 옆 칸·뒤 칸은 캡처 값 그대로(토큰 칸 생략)."""
    ref = _ld(1, path.encode()) + _vi(2, kind) + _ld(4, mime) + _ld(5, FILE_ID)
    item = _ld(1, ref) + _ld(2, name.encode())
    holder = _ld(1, b"") + _vi(2, 0) + _ld(4, item) + _vi(7, 0)
    inner = _ld(1, holder) + _ld(2, _ld(1, b"ko")) + _vi(8, 1) + _vi(11, 1) + _vi(80, 3)
    return _ld(1, _ld(8, _ld(5, _ld(1, inner) + _vi(4, 1))))


def _send_config(a, config):
    body = f"count=1&ofs=0&req0___data__={quote(base64.b64encode(config).decode(), safe='=')}"
    f = tflow.tflow(req=tutils.treq(method="POST", host=addon_mod.GEMINI_PROMPT_HOST,
                                    path="/v1/processSession?VER=8&SID=s&RID=2", content=body.encode()))
    asyncio.run(a.request(f))
    assert f.response is None
    msg = pb.decode(base64.b64decode(unquote(f.request.get_text().split("req0___data__=")[1])))
    return {path: fld for path, fld in msg.walk()}


def test_그림_첨부_항목은_종류도_문서로_바꾼다():
    a = CampfireAddon()
    a._gemini_files[PATH] = "image_e5cbe4_masked.md"
    got = _send_config(a, _captured_config(1, b"image/png", "image_e5cbe4.png"))
    assert got[(*ITEM, 1, 2)].varint == 10, "종류가 그림(1)으로 남으면 Gemini 가 가린 글을 그림으로 읽는다"
    assert got[(*ITEM, 1, 4)].value == b"text/plain"
    assert got[(*ITEM, 2)].value == b"image_e5cbe4_masked.md"
    assert got[(*ITEM, 1, 1)].value == PATH.encode() and got[(*ITEM, 1, 5)].value == FILE_ID
    assert got[(1, 8, 5, 1, 1, 7)].varint == 0 and got[(1, 8, 5, 4)].varint == 1, "다른 칸은 그대로"


def test_문서_첨부의_종류는_그대로다():
    a = CampfireAddon()
    a._gemini_files[PATH] = "pii_masked.md"
    got = _send_config(a, _captured_config(10, DOCX, "pii.docx"))
    assert got[(*ITEM, 1, 2)].varint == 10 and got[(*ITEM, 1, 4)].value == b"text/plain"


def test_우리가_바꾸지_않은_그림은_건드리지_않는다():
    a = CampfireAddon()
    a._gemini_files["/contrib_service/ttl_1d/other"] = "x_masked.md"
    got = _send_config(a, _captured_config(1, b"image/png", "image_e5cbe4.png"))
    assert got[(*ITEM, 1, 2)].varint == 1 and got[(*ITEM, 1, 4)].value == b"image/png"
