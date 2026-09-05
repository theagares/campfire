"""파싱 단계가 엔진 전체를 붙잡거나, 검사하지 않은 것을 "검사했다"고 말하지 않게 한다.

코드 리뷰에서 네 가지가 한 자리에 겹쳐 있었다.

  1) REQUEST_TIMEOUT_SEC / STATUS_TIMEOUT 이 정의만 되고 아무 데서도 안 쓰였다 —
     타임아웃이 있는 것처럼 보이지만 없었다.
  2) 추출된 텍스트 길이에 상한이 없었다. 업로드는 바이트로 막지만 "풀린 길이" 는
     아무도 안 봐서, 청크 수가 그대로 따라 늘고 청크마다 추론이 붙었다.
  3) 확장자·MIME 이 안 맞으면 무조건 txt 로 폴백했는데 extract_txt 는 최후에
     errors="replace" 로 무조건 성공한다 — zip/png/exe 가 STATUS_OK 로 나왔다.
     사용자에겐 정상 검사로 보이지만 검사된 건 모지바케다.
  4) parse_document(동기)를 async 안에서 그대로 불렀다. 무거운 PDF 를 파서가 붙들고
     있는 동안 이벤트 루프 전체가 멎어 /health·SSE·처리현황이 같이 멈췄다.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import config
from app.core import model_status
from app.core.parser import STATUS_OK, STATUS_TIMEOUT, STATUS_UNSUPPORTED, parse_document
from app.core.pipeline import orchestrator as orch


def _parse_only(monkeypatch):
    """탐지 단계를 끄고 파싱/자르기만 보게 한다.

    이 파일의 관심사는 파싱이다. 모델 가중치가 로컬에 있느냐에 따라 결과가 달라지면
    같은 코드가 기계마다 다르게 통과한다 — 게이트를 명시적으로 닫고 본다(모델 미준비
    경로도 truncated 를 그대로 실어 보내야 하므로 계약은 유지된다)."""
    monkeypatch.setattr(model_status, "all_ready", lambda: False)


# ── (3) 바이너리를 "검사 완료" 로 표시하지 않는다 ────────────────────────────
@pytest.mark.parametrize(
    "label, data, mime, name",
    [
        ("png", bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(range(256)), "image/png", "logo.png"),
        ("exe", b"MZ\x90\x00\x03" + bytes(range(256)) * 2, "application/octet-stream", "setup.exe"),
    ],
)
def test_binary_is_reported_unsupported_not_ok(label, data, mime, name):
    """모르면 '검사했다' 고 하지 않는다.

    STATUS_OK 로 나오면 사이드패널이 "검사되지 않았습니다" 고지를 띄우지 않아,
    사용자는 검사된 줄 알고 전송한다 — 실제로 본 건 깨진 문자열인데."""
    _text, status, reason = parse_document(data, mime, name)
    assert status == STATUS_UNSUPPORTED, f"{label}: {status} 로 나왔다 (reason={reason})"


@pytest.mark.parametrize(
    "label, data, mime, name",
    [
        ("확장자 없는 utf-8", "주민번호 900101-1234567".encode("utf-8"), "", "memo"),
        ("cp949", "한글 문서입니다".encode("cp949"), "", "memo.dat"),
        ("text/* MIME", b"hello world", "text/plain", "noext"),
    ],
)
def test_real_text_still_parses(label, data, mime, name):
    """폴백을 좁히느라 진짜 텍스트까지 막으면 안 된다."""
    text, status, _reason = parse_document(data, mime, name)
    assert status == STATUS_OK, f"{label}: {status}"
    assert text


# ── (2) 추출 텍스트 길이 상한 ────────────────────────────────────────────────
def test_long_text_is_truncated_and_announced(monkeypatch):
    """상한을 넘으면 앞부분만 검사하되, 잘랐다는 사실을 결과와 경고로 알린다.

    조용히 자르면 사용자는 문서 전체가 검사된 줄 안다 — 뒷부분에 있던 주민번호가
    그대로 나가도 모른다."""
    _parse_only(monkeypatch)
    monkeypatch.setattr(config, "MAX_TEXT_CHARS", 1000)
    body = ("가" * 5000).encode("utf-8")

    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    result = asyncio.run(
        orch.run_pipeline(file_bytes=body, mime_type="text/plain", file_name="long.txt", emit=emit)
    )

    assert result["truncated"] is True, "잘렸는데 truncated 가 안 켜졌다"
    assert len(result["originalText"]) == 1000

    warn = [e for e in events if e.get("type") == "warning" and e.get("partial")]
    assert warn, f"잘림 경고가 안 나갔다: {[e.get('type') for e in events]}"
    assert "5,000" in warn[0]["reason"], warn[0]["reason"]


def test_short_text_is_not_marked_truncated(monkeypatch):
    """상한 안쪽 문서에 truncated 가 켜지면 사용자가 헛되이 불안해한다."""
    _parse_only(monkeypatch)
    result = asyncio.run(
        orch.run_pipeline(file_bytes=b"hello", mime_type="text/plain", file_name="a.txt")
    )
    assert result["truncated"] is False


# ── (1) 파싱 타임아웃이 실제로 동작한다 ──────────────────────────────────────
def test_slow_parse_times_out(monkeypatch):
    """상한을 넘긴 파싱은 STATUS_TIMEOUT 으로 '미검사 통과' 된다.

    예전엔 PARSE_TIMEOUT_SEC(구 REQUEST_TIMEOUT_SEC)도 STATUS_TIMEOUT 도 만드는
    쪽이 없어 둘 다 죽어 있었다 — 있는 척하는 안전장치가 제일 위험하다."""
    monkeypatch.setattr(config, "PARSE_TIMEOUT_SEC", 0.2)

    def _slow(*_a, **_k):
        time.sleep(3)
        return "느림", STATUS_OK, None

    monkeypatch.setattr(orch, "parse_document", _slow)

    result = asyncio.run(
        orch.run_pipeline(file_bytes=b"x", mime_type="text/plain", file_name="slow.txt")
    )
    assert result["scanStatus"] == STATUS_TIMEOUT, result["scanStatus"]
    assert result["blocked"] is False, "미검사 통과 정책(PLAN §9.2)이라 막지는 않는다"


# ── (4) 파싱이 이벤트 루프를 막지 않는다 ─────────────────────────────────────
def test_parse_does_not_block_event_loop(monkeypatch):
    """파싱이 도는 동안에도 다른 태스크가 계속 돌아야 한다.

    이게 깨지면 무거운 PDF 하나가 /health·SSE·처리현황을 통째로 멈춘다 —
    데스크탑 앱이 엔진이 죽은 걸로 오인할 수 있다."""
    _parse_only(monkeypatch)
    PARSE_SEC = 0.4

    def _slow(*_a, **_k):
        time.sleep(PARSE_SEC)
        return "본문", STATUS_OK, None

    monkeypatch.setattr(orch, "parse_document", _slow)

    async def scenario():
        ticks = 0
        running = True

        async def ticker():
            nonlocal ticks
            while running:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        await orch.run_pipeline(file_bytes=b"x", mime_type="text/plain", file_name="a.txt")
        got = ticks
        running = False
        task.cancel()
        return got

    ticks = asyncio.run(scenario())
    # 파싱을 스레드로 뺐다면 그 0.4초 동안 ticker 가 수십 번 돈다.
    # 이벤트 루프를 막고 있었다면 한 자리수(사실상 0)에 머문다.
    assert ticks >= 10, f"파싱 중 이벤트 루프가 멎었다 (tick={ticks})"
