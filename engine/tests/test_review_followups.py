"""코드리뷰 2026-08-05 후속 수정(5~16번) 회귀 테스트.

앞선 4건(test_local_attack_surface.py)과 달리 여기 모인 건 대체로 "조용히 잘못
동작하던" 것들이다 — 예외가 안 나므로 테스트 없이는 되돌아가도 아무도 모른다.
"""

from __future__ import annotations

import asyncio
import tarfile
from pathlib import Path

import pytest

from app.adapters.http_api import models as models_router
from app.adapters.mcp import tools
from app.core.masker import masker


# ── 8. 외부 입력(mask_text)이 500 을 내지 않아야 한다 ─────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        {"start": 0, "end": 3},                       # type 없음
        {"type": None, "start": 0, "end": 3},         # type 이 문자열이 아님
        {"type": 123, "start": 0, "end": 3},
        {"type": "EMAIL", "start": "0", "end": 3},    # 좌표가 문자열
        {"type": "EMAIL"},                            # 좌표 없음
        "문자열 항목",                                 # dict 조차 아님
        None,
    ],
)
def test_mask_text_survives_malformed_items(bad):
    """mask_text 의 항목 목록은 AI 가 채워 보내는 값이다. 하나 틀렸다고 전체가
    KeyError/TypeError 로 죽으면 안 된다."""
    out = asyncio.run(tools.mask_text("연락처 a@b.com 입니다", [bad]))
    assert isinstance(out["maskedText"], str)
    assert out["skippedCount"] >= 1


def test_mask_text_still_masks_valid_items():
    """방어를 넣다가 정상 항목까지 버리면 안 된다(마스킹 없이 성공하는 게 최악)."""
    text = "연락처 a@b.com 입니다"
    start = text.index("a@b.com")
    out = asyncio.run(tools.mask_text(text, [
        {"type": "EMAIL", "start": start, "end": start + len("a@b.com"), "text": "a@b.com"},
    ]))
    assert "a@b.com" not in out["maskedText"]
    assert out["maskedText"] == "연락처 [이메일 마스킹] 입니다"
    assert out["skippedCount"] == 0


def test_masker_drops_items_without_type():
    """core 마스커 자체도 모양이 어긋난 항목을 버텨야 한다(MCP 뿐 아니라 모든 경로)."""
    out = masker.apply_masking("hello world", [
        {"start": 0, "end": 5},
        {"type": "EMAIL", "start": 6, "end": 11},
    ])
    assert len(out["applied"]) == 1
    assert out["applied"][0]["type"] == "EMAIL"


def test_masker_handles_none_confidence_on_overlap():
    """MCP _redact_items 를 거친 항목은 confidence 가 None 으로 올 수 있다 —
    겹침 비교에서 None > float 로 터지면 안 된다."""
    out = masker.apply_masking("0123456789abcd", [
        {"type": "PHONE", "start": 0, "end": 5, "confidence": None},
        {"type": "ID_NUMBER", "start": 2, "end": 12, "confidence": None},
    ])
    assert len(out["applied"]) == 1
    assert (out["applied"][0]["start"], out["applied"][0]["end"]) == (0, 12)


# ── 14. tar 추출이 링크 멤버를 거부해야 한다 ──────────────────────────────────
def test_safe_extract_rejects_symlink_member(tmp_path):
    """이름만 검사하던 예전 방식은 심볼릭 링크 멤버를 못 잡았다."""
    archive = tmp_path / "evil.tar"
    outside = tmp_path / "outside"
    outside.mkdir()
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = str(outside)
        tar.addfile(info)

    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(archive) as tar:
        with pytest.raises(Exception):
            models_router._safe_extract(tar, dest)


def test_safe_extract_accepts_normal_member(tmp_path):
    """거부만 하면 다운로드가 통째로 깨진다 — 정상 tar 는 그대로 풀려야 한다."""
    payload = tmp_path / "weights.bin"
    payload.write_bytes(b"x" * 16)
    archive = tmp_path / "ok.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(payload, arcname="models/weights.bin")

    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(archive) as tar:
        models_router._safe_extract(tar, dest)
    assert (dest / "models" / "weights.bin").read_bytes() == b"x" * 16


# ── 15. 진행 이벤트 스로틀 ────────────────────────────────────────────────────
def test_progress_interval_is_configured():
    assert models_router._PROGRESS_MIN_INTERVAL_SEC > 0


# ── 7. secure_search_files 는 파일당 한 번만 검사해야 한다 ────────────────────
def test_search_runs_pipeline_once_per_file(tmp_path, monkeypatch):
    """예전엔 매칭 라인마다 run_pipeline 을 불렀다 — 라인당 ML 추론 + Solar 과금."""
    target = tmp_path / "notes.txt"
    target.write_text(
        "\n".join([
            "연락처 010-1234-5678 담당",
            "관계없는 줄",
            "연락처 a@b.com 담당",
            "연락처 또 하나",
        ]),
        encoding="utf-8",
    )

    calls: list[str] = []

    async def fake_pipeline(*, text, file_name="", wrap_file=False, **_kw):
        calls.append(text)
        phone = text.find("010-1234-5678")
        items = []
        if phone >= 0:
            items.append({"type": "PHONE", "start": phone, "end": phone + 13,
                          "text": "010-1234-5678", "confidence": 0.9, "source": "encoder"})
        return {
            "originalText": text, "maskedText": text,
            "piiItems": items, "injectionItems": [],
            "blocked": False, "scanStatus": "ok", "reason": None,
            "stats": {"piiCount": len(items), "injectionCount": 0},
            "policy": {"injection": "mask"},
        }

    monkeypatch.setattr(tools, "run_pipeline", fake_pipeline)

    out = asyncio.run(tools.secure_search_files(str(tmp_path), "연락처", pattern="*.txt"))

    assert len(calls) == 1, f"파일 1개인데 파이프라인을 {len(calls)}번 돌렸다"
    assert out["count"] == 3
    # 파일 전체를 한 번 검사했더라도 스니펫은 그 라인 기준으로 마스킹돼야 한다.
    first = next(r for r in out["results"] if r["line"] == 1)
    assert "010-1234-5678" not in first["lineText"]
    assert first["lineText"] == "연락처 [전화번호 마스킹] 담당"
    # 그 라인에 탐지가 없으면 원문 그대로.
    third = next(r for r in out["results"] if r["line"] == 3)
    assert third["lineText"] == "연락처 a@b.com 담당"


def test_search_skips_pipeline_when_nothing_matches(tmp_path, monkeypatch):
    """검색어가 없는 파일에 추론 비용을 쓰지 않아야 한다."""
    (tmp_path / "notes.txt").write_text("아무 내용", encoding="utf-8")

    calls: list[str] = []

    async def fake_pipeline(**kw):
        calls.append(kw.get("text", ""))
        raise AssertionError("매칭이 없는데 파이프라인을 돌렸다")

    monkeypatch.setattr(tools, "run_pipeline", fake_pipeline)
    out = asyncio.run(tools.secure_search_files(str(tmp_path), "없는말", pattern="*.txt"))
    assert out["count"] == 0
    assert calls == []


def test_search_withholds_snippets_when_blocked(tmp_path, monkeypatch):
    """block 정책이면 위치는 알려주고 내용은 주지 않는다."""
    (tmp_path / "memo.txt").write_text("연락처 무언가", encoding="utf-8")

    async def fake_pipeline(*, text, **_kw):
        return {
            "originalText": text, "maskedText": text,
            "piiItems": [], "injectionItems": [
                {"type": "OTHER_INJECTION", "start": 0, "end": len(text),
                 "text": text, "confidence": 0.99, "source": "llm"}],
            "blocked": True, "scanStatus": "ok", "reason": None,
            "stats": {"piiCount": 0, "injectionCount": 1},
            "policy": {"injection": "block"},
        }

    monkeypatch.setattr(tools, "run_pipeline", fake_pipeline)
    out = asyncio.run(tools.secure_search_files(str(tmp_path), "연락처", pattern="*.txt"))
    assert out["count"] == 1
    assert out["results"][0]["blocked"] is True
    assert out["results"][0]["lineText"] == "[차단됨 — 인젝션 정책]"


def test_search_skips_secret_files(tmp_path, monkeypatch):
    """자격증명 파일은 검색 스니펫으로도 새지 않아야 한다(2번과 같은 이유)."""
    (tmp_path / ".env").write_text("SECRET_TOKEN=연락처값", encoding="utf-8")

    async def fake_pipeline(**_kw):
        raise AssertionError(".env 를 검사했다")

    monkeypatch.setattr(tools, "run_pipeline", fake_pipeline)
    out = asyncio.run(tools.secure_search_files(str(tmp_path), "연락처", pattern="*"))
    assert out["count"] == 0


# ── 5. 프로세스가 회수됐을 때의 재시도 ───────────────────────────────────────
def test_detect_retries_once_when_process_was_reclaimed():
    """유휴 워처가 _ensure_process 와 _infer 사이에 프로세스를 죽여도, 요청 하나가
    500 으로 죽는 게 아니라 다시 띄우고 이어가야 한다."""
    from app.core.detectors.injection import llm_mcp

    det = llm_mcp.InjectionLlmMcpDetector()
    ensure_calls = {"n": 0}
    infer_calls = {"n": 0}

    async def fake_ensure():
        ensure_calls["n"] += 1

    async def fake_infer(_text, user_prompt=None):
        infer_calls["n"] += 1
        if infer_calls["n"] == 1:
            raise llm_mcp.ProcessGone("죽어 있었다")
        return {"is_injection": False, "scores": {}}

    det._ensure_process = fake_ensure          # type: ignore[method-assign]
    det._infer = fake_infer                    # type: ignore[method-assign]

    result = asyncio.run(det.detect("검사할 텍스트"))
    assert result == []
    assert infer_calls["n"] == 2, "ProcessGone 인데 재시도하지 않았다"
    assert ensure_calls["n"] == 2, "재시도 전에 프로세스를 다시 띄우지 않았다"


def test_detect_does_not_retry_on_other_errors():
    """타임아웃(응답 없음)까지 재시도하면 120초를 두 번 기다리게 된다."""
    from app.core.detectors.injection import llm_mcp

    det = llm_mcp.InjectionLlmMcpDetector()
    infer_calls = {"n": 0}

    async def fake_ensure():
        pass

    async def fake_infer(_text, user_prompt=None):
        infer_calls["n"] += 1
        raise RuntimeError("추론 응답이 120초 안에 오지 않음")

    det._ensure_process = fake_ensure          # type: ignore[method-assign]
    det._infer = fake_infer                    # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        asyncio.run(det.detect("검사할 텍스트"))
    assert infer_calls["n"] == 1


# ── 4. 추론 stdio 프로토콜(타임아웃·id 대조)을 모델 없이 검증한다 ────────────
#
# 실모델(EXAONE 2.4GB)을 띄우면 이 기계에서는 커밋 메모리가 모자라 로딩 자체가
# 실패한다(os error 1455). 검증하려는 건 모델이 아니라 우리 프로토콜 처리라,
# 같은 JSONL 규약을 말하는 최소 스텁을 서브프로세스로 띄워 그 부분만 태운다.
_STUB = r'''
import json, sys, time
mode = sys.argv[-1]
print(json.dumps({"ready": True, "device": "cpu"}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    if mode == "silent":
        time.sleep(60)          # 응답을 영영 주지 않는다
        continue
    if mode == "noise":
        print("[ERROR] transformers 진단 잡음", flush=True)
    if mode == "stale":
        # 이전 요청의 지각 응답을 먼저 흘린다 — 이걸 이번 결과로 받아들이면 안 된다.
        print(json.dumps({"id": "이건-다른-요청", "is_injection": True,
                          "scores": {"misaligned": 1.0}}), flush=True)
    print(json.dumps({"id": req.get("id"), "is_injection": False, "scores": {}}), flush=True)
'''


def _run_stub(tmp_path, mode, monkeypatch, timeout_sec=30.0):
    """스텁을 띄워 detect() 를 한 번 돌리고 결과를 돌려준다."""
    import sys

    from app.core.detectors.injection import llm_mcp

    script = tmp_path / "stub_runtime.py"
    script.write_text(_STUB, encoding="utf-8")

    monkeypatch.setattr(llm_mcp.config, "INJECTION_PYTHON_EXECUTABLE", sys.executable)
    monkeypatch.setattr(llm_mcp.config, "INJECTION_INFER_TIMEOUT_SEC", timeout_sec)
    monkeypatch.setattr(llm_mcp.config, "INJECTION_LLM_LOAD_DELAY_SEC", 30.0)
    monkeypatch.setattr(llm_mcp.config, "INJECTION_LOCALIZE_ENABLED", False)
    monkeypatch.setattr(llm_mcp.backbone, "is_cached", lambda: False)
    # 스텁은 argv 마지막 값으로 동작 모드를 읽는다. --stdio 뒤에 붙도록 max-seq-len 이
    # 아니라 스크립트 경로 뒤가 아니어야 해서, 인자 조립을 감싸 모드를 맨 끝에 둔다.
    monkeypatch.setattr(llm_mcp.config, "INJECTION_MAX_SEQ_LEN", 4096)

    det = llm_mcp.InjectionLlmMcpDetector()
    monkeypatch.setattr(det, "_runtime_script", lambda: script)

    real_exec = asyncio.create_subprocess_exec

    async def exec_with_mode(*args, **kwargs):
        return await real_exec(*args, mode, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_with_mode)

    async def scenario():
        try:
            return await det.detect("검사할 텍스트")
        finally:
            await det.aclose()

    return asyncio.run(scenario())


def test_infer_ignores_stale_response_with_other_id(tmp_path, monkeypatch):
    """타임아웃을 넣은 뒤에는 '늦게 도착한 이전 응답'이 존재할 수 있다.

    id 를 대조하지 않으면 그 응답(여기서는 is_injection=True)을 이번 청크의 결과로
    받아들여, 엉뚱한 청크의 판정으로 문서를 마스킹한다.
    """
    out = _run_stub(tmp_path, "stale", monkeypatch)
    assert out == [], "다른 요청의 응답을 이번 결과로 받아들였다"


def test_infer_skips_non_json_noise(tmp_path, monkeypatch):
    """transformers 가 stdout 에 print 하는 진단 잡음은 건너뛰어야 한다(기존 계약)."""
    assert _run_stub(tmp_path, "noise", monkeypatch) == []


def test_infer_gives_up_instead_of_hanging_forever(tmp_path, monkeypatch):
    """응답이 오지 않으면 영원히 기다리지 않고 실패해야 한다.

    예전에는 _request_lock 을 쥔 채 readline() 을 무한 대기해서, 이 상태가 되면
    이후 모든 인젝션 검사가 함께 멎었다.
    """
    with pytest.raises(RuntimeError, match="오지 않음"):
        _run_stub(tmp_path, "silent", monkeypatch, timeout_sec=1.5)


def test_infer_normal_path_still_works(tmp_path, monkeypatch):
    """방어를 넣다가 정상 응답 경로를 막지 않았는지."""
    assert _run_stub(tmp_path, "normal", monkeypatch) == []


# ── 6. Solar HTTP 클라이언트는 하나만 만들어져야 한다 ────────────────────────
def test_solar_http_client_created_once_under_concurrency(monkeypatch):
    """청크를 동시에 돌리면 lazy 생성이 겹쳐 클라이언트가 두 개 만들어졌고,
    하나는 참조를 잃은 채 커넥션 풀로 남았다."""
    from app.core.detectors.injection import llm_mcp

    det = llm_mcp.InjectionLlmMcpDetector()
    monkeypatch.setattr(llm_mcp.config, "INJECTION_LOCALIZE_ENABLED", True)

    created = {"n": 0}

    class FakeClient:
        def __init__(self, **_kw):
            created["n"] += 1

        async def post(self, *_a, **_kw):
            await asyncio.sleep(0)
            raise RuntimeError("네트워크 없음")  # None 반환 경로로 떨어진다

    monkeypatch.setattr(llm_mcp.httpx, "AsyncClient", FakeClient)

    async def drive():
        # 서로 다른 텍스트라 캐시가 안 겹친다 = 둘 다 클라이언트를 필요로 한다.
        return await asyncio.gather(
            det._localize_with_solar("첫 번째 청크"),
            det._localize_with_solar("두 번째 청크"),
        )

    out = asyncio.run(drive())
    assert out == [None, None]  # 네트워크 실패 → 판단 불가(청크 전체 마스킹 fail-safe)
    assert created["n"] == 1, f"클라이언트가 {created['n']}개 만들어졌다"
