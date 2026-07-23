"""
app/core/detectors/pii/encoder.py
PII 인코더 모델 교체 슬롯 스텁 (PLAN §5 교체 시나리오, §10 Phase 6).

실제 SKT A.X Encoder 모델은 여기에 로드/추론 로직을 넣는다(문장 임베딩 +
분류 헤드로 정규식만으로는 잡기 어려운 문맥적 PII 까지 탐지하는 용도). 지금은
진짜 모델을 로드하지 않는 최소 스텁이다 — 목적은 "설정에서 pii: encoder 로
바꾸면 registry 가 이 스텁으로 전환되고, 같은 Detection 인터페이스를 반환하며,
코어/어댑터/익스텐션이 무변경으로 동작하는가"를 증명하는 것.

내부적으로는 rule_based 와 동일한 룰 테이블(pii_ko.yaml)을 재사용하되
source 만 "encoder" 로 표시한다(PLAN §5 Detection.source: "rule"|"encoder"|"llm").

GPU 상주 정책(PLAN §4.1): PII 인코더 = 항시 상주. 이 스텁도 생성 즉시
GpuResidency.mark_loaded_immediately() 로 "로드 완료" 상태가 되어, 어떤 요청도
로드 대기 없이 즉답한다(룰베이스 v1과 동일하게 대기가 없음이 정책의 핵심).
"""

from __future__ import annotations

from pathlib import Path

from app import config

from ..base import ChunkMeta, Detection
from ..gpu_residency import GpuResidency
from ..rule_engine import compile_rules, run_rules


class EncoderStubDetector:
    """PII 인코더 교체 슬롯 스텁 — Detector Protocol 구현체."""

    name = "pii_encoder"
    kind = "pii"

    def __init__(self, yaml_path: Path):
        self._rules = compile_rules(yaml_path)
        self.residency = GpuResidency(
            model_name=self.name,
            mode="always_on",
            idle_timeout_sec=None,
            load_delay_sec=0.0,
        )
        # PII 인코더 = 항시 상주(PLAN §4.1): 앱 기동(= registry.load_detectors() 호출)
        # 시점에 즉시 로드 완료 상태로 만든다.
        self.residency.mark_loaded_immediately()

    async def detect(self, text: str, *, meta: ChunkMeta | None = None) -> list[Detection]:
        # TODO(실제 모델 교체 시): 여기서 SKT A.X Encoder 로 임베딩/분류 추론을 수행.
        # 지금은 스텁이므로 rule 테이블을 그대로 돌려 같은 Detection 스키마만 보장한다.
        await self.residency.ensure_loaded()  # always_on 이라 항상 즉시 반환(대기 없음)
        dets = run_rules(self._rules, text, source="encoder")
        self.residency.touch()
        return dets


def build() -> EncoderStubDetector:
    return EncoderStubDetector(config.RULES_DIR / "pii_ko.yaml")
