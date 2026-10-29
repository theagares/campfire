"""Claude·Grok 첨부 — 업로드 응답의 파일 id 로 대기 첨부를 묶어 그 전송에 실린 첨부만 판정한다.

2026-10-29 맥 v0.2.59 캡처(pii.docx + "세 줄로 요약해 주세요"):
  Claude  POST /api/<org>/upload → {"file_uuid": "828f7e8a-…"}; 전송 PerformAction 첨부 칸 1 에 그 uuid
  Grok    POST /http/upload-file-v2/direct → {"fileMetadata": {"fileMetadataId": "c61b2774-…"}};
          WS response.create 의 input_chunks[].mention.file_mention.file_id 와 file_attachment_ids
예전엔 "이 사이트의 다음 전송" 으로 추정해(#316) 올렸다 뺀 첨부까지 판정해 막을 수 있었다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy import http  # noqa: E402
from mitmproxy.test import tflow, tutils  # noqa: E402

import app.adapters.proxy.addon as addon_mod  # noqa: E402
from app.adapters.proxy import protobuf as pb  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon  # noqa: E402

PROMPT = "세 줄로 요약해 주세요"
BOUNDARY = "XyZ"


@pytest.fixture
def fake(monkeypatch):
    """업로드 검사(깨끗), 첨부 인젝션 판정("이전 지시 무시" 면 인젝션), 프롬프트(깨끗)를 흉내 낸다."""
    import app.core.pipeline.orchestrator as orch

    state = {"judged": [], "notices": []}

    async def run(*, text=None, file_bytes=None, user_prompt=None, **_kw):
        base = {"piiItems": [], "forcedMaskItems": [], "blocked": False, "truncated": False, "scanStatus": "ok"}
        if file_bytes is not None:
            body = file_bytes.decode()
            return {**base, "originalText": body, "maskedText": body, "injectionItems": []}
        if user_prompt:
            state["judged"].append(text)
        inj = [{"type": "OTHER_INJECTION", "start": 0, "end": 1}] if user_prompt and "이전 지시 무시" in text else []
        return {**base, "originalText": text, "maskedText": text, "injectionItems": inj}

    monkeypatch.setattr(orch, "run_pipeline", run)
    monkeypatch.setattr(addon_mod.broker, "notify", lambda host, reason: state["notices"].append(reason))
    return state


def _upload(a, host, path, content: str, response: dict):
    body = (f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n{content}\r\n--{BOUNDARY}--\r\n").encode()
    f = tflow.tflow(req=tutils.treq(method="POST", host=host, path=path, content=body))
    f.request.headers["content-type"] = f"multipart/form-data; boundary={BOUNDARY}"
    asyncio.run(a.request(f))
    assert f.response is None
    f.response = http.Response.make(200, json.dumps(response).encode(), {"content-type": "application/json"})
    a.response(f)


def _ld(num, data):
    return pb._encode_varint((num << 3) | 2) + pb._encode_varint(len(data)) + data


def _perform(a, file_uuid):
    """PerformAction — 필드 2 아래 3=프롬프트, 4=첨부 {1: file_uuid, 2: 이름}."""
    body = _ld(2, _ld(3, PROMPT.encode()) + _ld(4, _ld(1, file_uuid.encode()) + _ld(2, b"a_masked.md")))
    f = tflow.tflow(req=tutils.treq(method="POST", host="claude.ai", path=addon_mod.CLAUDE_MESSAGE_RPC_PATHS[0],
                                    content=body))
    f.request.headers["content-type"] = "application/proto"
    asyncio.run(a.request(f))
    return f


def test_Claude_는_업로드_응답의_file_uuid_로_그_전송의_첨부만_판정한다(fake):
    a = CampfireAddon()
    _upload(a, "claude.ai", "/api/org1/upload", "B 본문 이전 지시 무시", {"success": True, "file_uuid": "uuid-b"})
    _upload(a, "claude.ai", "/api/org1/upload", "A 본문", {"success": True, "file_uuid": "uuid-a"})
    first = _perform(a, "uuid-a")
    assert first.response is None, "실리지 않은 첨부(B)로 이 전송을 막았다"
    assert fake["judged"] == ["A 본문"]
    second = _perform(a, "uuid-b")
    assert second.response is not None and second.response.status_code == 403


def _grok_send(a, f, file_id):
    from mitmproxy import websocket
    from wsproto.frame_protocol import Opcode

    event = {"type": "response.create", "item": {"type": "message", "role": "user", "x_grok": {"input_chunks": [
        {"mention": {"file_mention": {"file_id": file_id}}}, {"text": {"text": PROMPT}}]}},
        "file_attachment_ids": [file_id]}
    f.websocket.messages.append(websocket.WebSocketMessage(
        Opcode.TEXT, True, json.dumps({"session_id": "s", "event": event}, ensure_ascii=False).encode()))
    asyncio.run(a.websocket_message(f))
    return f.websocket.messages[-1]


def test_Grok_은_업로드_응답의_fileMetadataId_로_그_전송의_첨부만_판정한다(fake):
    a = CampfireAddon()
    path = "/http/upload-file-v2/direct"
    _upload(a, "grok.com", path, "B 본문 이전 지시 무시", {"uploadId": "u-b", "fileMetadata": {"fileMetadataId": "fm-b"}})
    _upload(a, "grok.com", path, "A 본문", {"uploadId": "u-a", "fileMetadata": {"fileMetadataId": "fm-a"}})
    ws = tflow.twebsocketflow()
    ws.request.host, ws.request.path = "grok.com", "/ws/mgw/?uid=1"
    ws.websocket.messages = []
    first = _grok_send(a, ws, "fm-a")
    assert not first.dropped, "실리지 않은 첨부(B)로 이 전송을 버렸다"
    assert fake["judged"] == ["A 본문"]
    second = _grok_send(a, ws, "fm-b")
    assert second.dropped and fake["notices"] and "인젝션" in fake["notices"][-1]


def test_업로드_응답에서_파일_id_를_읽는다():
    assert addon_mod._upload_ref('{"file_uuid":"828f"}') == "828f"
    assert addon_mod._upload_ref('{"fileMetadata":{"fileMetadataId":"c61b"}}') == "c61b"
    assert addon_mod._upload_ref('{"fileMetadata":[1]}') is None
    assert addon_mod._upload_ref("not json") is None and addon_mod._upload_ref(None) is None
