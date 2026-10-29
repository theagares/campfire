"""Gemini 그림 첨부 — 맥 v0.2.60 M-1: 그림을 가린 글(.md)로 바꿔 올렸는데 Gemini 가 그 글을 그림으로 읽어 엉터리 답을 냈다
(칩 "image_e69e3f_masked", .md 없음). 문서(docx·txt) 캡처로 맞춘 칸(ProcessFile head[3], 세션 설정 항목 1.4·2)만 바꿔서
그림에만 있는 "그림이다" 칸이 남은 것으로 보인다. 맥 캡처가 오면 PNG_CONFIG_B64 를 채우고 그 칸을 고친다.
그 전까지는 그 칸을 다음 시험 로그로 찾게 칸 구조를 글 없이 남긴다.
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

# 맥 캡처(그림 붙여넣기 → Enter 의 processSession ofs=0 req0___data__, 브라우저가 보낸 원래 값)가 오면 채운다.
PNG_CONFIG_B64: str | None = None
PNG_PATH: str | None = None  # 그 캡처의 "upload, finalize" 응답 본문(파일 경로)


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


@pytest.mark.skipif(PNG_CONFIG_B64 is None, reason="맥 캡처(그림 세션 설정) 대기 — 오면 채우고 그림 칸 고치기를 시험한다")
def test_그림_첨부_항목도_문서처럼_글로_보이게_바꾼다():
    a = CampfireAddon()
    a._gemini_files[PNG_PATH] = "image_masked.md"
    body = f"count=1&ofs=0&req0___data__={PNG_CONFIG_B64}"
    f = tflow.tflow(req=tutils.treq(method="POST", host=addon_mod.GEMINI_PROMPT_HOST,
                                    path="/v1/processSession?VER=8&SID=s&RID=2", content=body.encode()))
    asyncio.run(a.request(f))
    msg = pb.decode(base64.b64decode(unquote(f.request.get_text().split("req0___data__=")[1])))
    values = [fld.value for _, fld in msg.walk() if fld.value is not None]
    assert not any(v.startswith(b"image/") for v in values), "그림 형식이 남았다 — Gemini 가 가린 글을 그림으로 읽는다"
    assert "image_masked.md".encode() in values
    # TODO(캡처 후): 그림에만 있는 칸(문서와 다른 값)을 문서 값으로 바꿨는지 여기서 확인한다.
