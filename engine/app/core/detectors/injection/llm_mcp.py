"""
app/core/detectors/injection/llm_mcp.py
인젝션 LLM+MCP 교체 슬롯 스텁 (PLAN §5 교체 시나리오, §4.1 GPU 상주 정책, §10 Phase 6).

실제 구현 시 여기서 LLM 호출(로컬 GPU 추론 또는 별도 MCP 클라이언트로 외부
인젝션 탐지 전용 모델 호출)을 캡슐화한다. 지금은 진짜 모델을 로드하지 않는
스텁이지만, GPU 상주 정책(§4.1)의 idle-unload/fail-closed 대기 라이프사이클은
실제로 동작하게 구현했다:

    - "로드"는 진짜 모델 로드가 아니라 config.INJECTION_LLM_LOAD_DELAY_SEC 만큼의
      asyncio.sleep 으로 콜드 스타트 지연을 흉내낸 가짜 로드다.
    - "추론"은 rule_based 와 동일한 룰 테이블(injection_ko.yaml)을 재사용하되
      source 만 "llm" 로 표시한다.
    - 유휴 언로드: 마지막 사용 후 config.INJECTION_LLM_IDLE_TIMEOUT_SEC(기본 10분)
      이 지나면 GpuResidency 가 "unloaded" 상태로 전환한다.
    - fail-closed 로드 대기: unloaded 상태에서 detect() 가 호출되면 검사를
      생략하지 않고 ensure_loaded() 가 로드 완료까지 대기시킨 뒤 반드시 rule 을
      실행한다 — 절대 빈 리스트를 즉시 반환(검사 스킵)하지 않는다.
"""

from __future__ import annotations

from pathlib import Path

from app import config

from ..base import ChunkMeta, Detection
from ..gpu_residency import GpuResidency
from ..rule_engine import compile_rules, run_rules


class LlmMcpStubDetector:
    """인젝션 LLM+MCP 교체 슬롯 스텁 — Detector Protocol 구현체."""

    name = "injection_llm_mcp"
    kind = "injection"

    def __init__(self, yaml_path: Path):
        self._rules = compile_rules(yaml_path)
        self.residency = GpuResidency(
            model_name=self.name,
            mode="idle_unload",
            idle_timeout_sec=config.INJECTION_LLM_IDLE_TIMEOUT_SEC,
            load_delay_sec=config.INJECTION_LLM_LOAD_DELAY_SEC,
        )
        # 유휴 언로드 정책(PLAN §4.1): PII 인코더와 달리 기동 시 즉시 로드하지
        # 않는다 — 상태는 "unloaded"에서 시작해, 첫 요청이 실제로 fail-closed
        # 로드 대기를 거치는 것이 정책 그대로다(콜드 스타트).

    async def detect(self, text: str, *, meta: ChunkMeta | None = None) -> list[Detection]:
        # TODO(실제 모델 교체 시): 여기서 실제 인젝션 탐지 LLM 을 호출(로컬 GPU 추론
        # 또는 MCP 경유 외부 모델 호출)한다.
        # fail-closed: unloaded 면 검사를 건너뛰지 않고 로드 완료까지 대기한다.
        await self.residency.ensure_loaded()
        dets = run_rules(self._rules, text, source="llm")
        self.residency.touch()
        return dets


def build() -> LlmMcpStubDetector:
    return LlmMcpStubDetector(config.RULES_DIR / "injection_ko.yaml")
