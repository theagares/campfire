"""사용자가 AI 사이트에 직접 쓴 프롬프트(/jobs/prompt)의 인젝션 판정.

맥 실측: "이 문서를 한 줄로 요약해 줘" 를 보내면 인젝션으로 판정돼 문장 전체가
"[인젝션 마스킹]" 으로 바뀌었고, AI 에는 사용자 요청이 사라진 채 전달됐다. 원인은
프롬프트를 문서와 같은 틀("문서를 처리하는 AI 에게 내리는 지시 = 인젝션")로 본 것 —
프롬프트는 원래 AI 에게 내리는 지시다. 프롬프트 검사는 2차(Solar)에 사용자 본인의
작업 요청은 인젝션이 아니라는 기준을 따로 준다.
"""

import asyncio

from app import config
from app.adapters.http_api import jobs
from app.core import forced_mask, model_status
from app.core.detectors import registry
from app.core.detectors.injection.llm_mcp import InjectionLlmMcpDetector
from app.core.pipeline.orchestrator import run_pipeline

_DIRECT = InjectionLlmMcpDetector._SOLAR_DIRECT_PROMPT_SYSTEM_PROMPT
_DOC = InjectionLlmMcpDetector._SOLAR_SYSTEM_PROMPT


class _Solar:
    def __init__(self, content: str):
        self.sent: list[dict] = []
        self._content = content

    async def post(self, _url, headers=None, json=None):  # noqa: A002
        self.sent.append(json)
        content = self._content

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": content}}]}

        return _Resp()


class _NoPii:
    async def detect(self, text, *, meta=None):
        return []


def _pipeline_with(monkeypatch, solar_answer: str) -> _Solar:
    """1차(EXAONE)는 늘 misaligned 라고 답하게 두고 2차(Solar)만 가짜로 바꾼다."""

    async def _ready():
        pass

    async def _misaligned(text, user_prompt=None):
        return {"is_injection": True, "scores": {"misaligned": 1.0}}

    forced_mask.registry.configure([])
    monkeypatch.setattr(model_status, "all_ready", lambda: True)
    monkeypatch.setattr(config, "INJECTION_LOCALIZE_ENABLED", True)
    monkeypatch.setattr(config, "COMBINED_GPU_WORKER_ENABLED", False)
    detector = InjectionLlmMcpDetector()
    solar = _Solar(solar_answer)
    detector._http_client = solar
    monkeypatch.setattr(detector, "_ensure_process", _ready)
    monkeypatch.setattr(detector, "_infer", _misaligned)
    monkeypatch.setattr(registry, "get_injection_detector", lambda: detector)
    monkeypatch.setattr(registry, "get_pii_detector", lambda: _NoPii())
    return solar


def test_direct_prompt_criteria_pass_user_tasks_but_keep_attacks():
    assert "이 문서를 한 줄로 요약해 줘" in _DIRECT, "실제로 오탐 난 문장을 예시로 박아 둔다"
    assert "인젝션이 아닙니다" in _DIRECT
    assert "시스템 프롬프트" in _DIRECT and "무시" in _DIRECT
    assert '{"spans"' in _DIRECT and "복사" in _DIRECT  # _parse_solar_spans·_find_span 계약


def test_prompt_scan_uses_direct_criteria_and_solar_can_clear_it(monkeypatch):
    solar = _pipeline_with(monkeypatch, '{"spans": []}')
    prompt = "이 문서를 한 줄로 요약해 줘"

    result = asyncio.run(run_pipeline(text=prompt, direct_prompt=True))

    [request] = solar.sent
    assert request["messages"][0]["content"] == _DIRECT
    assert prompt in request["messages"][1]["content"]
    assert result["injectionItems"] == []
    assert result["maskedText"] == prompt


def test_document_scan_keeps_document_criteria(monkeypatch):
    solar = _pipeline_with(monkeypatch, '{"spans": []}')

    asyncio.run(run_pipeline(text="이 문서를 한 줄로 요약해 줘"))

    assert solar.sent[0]["messages"][0]["content"] == _DOC


def test_same_text_is_not_cached_across_criteria(monkeypatch):
    # 같은 문장이라도 문서 기준과 프롬프트 기준의 답은 다르다 — 한쪽 답을 재사용하면 안 된다.
    monkeypatch.setattr(config, "INJECTION_LOCALIZE_ENABLED", True)
    det = InjectionLlmMcpDetector()
    det._http_client = _Solar('{"spans": []}')
    text = "이 파일을 한 줄로 요약해 줘"

    async def scenario():
        await det._localize_with_solar(text)
        await det._localize_with_solar(text, direct_prompt=True)
        await det._localize_with_solar(text, direct_prompt=True)

    asyncio.run(scenario())
    keys = [call["prompt_cache_key"] for call in det._http_client.sent]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_jobs_prompt_endpoint_marks_direct_prompt(monkeypatch):
    seen = {}

    async def _fake_pipeline(**kwargs):
        seen.update(kwargs)
        return {"maskedText": kwargs["text"]}

    monkeypatch.setattr(jobs, "run_pipeline", _fake_pipeline)
    monkeypatch.setattr(jobs, "_record", lambda *a, **k: None)

    asyncio.run(jobs.create_prompt_job(text="이 문서를 한 줄로 요약해 줘"))

    assert seen["direct_prompt"] is True
