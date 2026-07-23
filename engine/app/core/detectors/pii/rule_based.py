"""
app/core/detectors/pii/rule_based.py
한국어 PII 룰베이스 detector (PLAN §5, §rules/pii_ko.yaml).

교체 시나리오: encoder.py 를 추가하고 같은 Detection 을 반환하면
설정(registry)에서 pii: encoder 로 전환만 하면 된다.
"""

from __future__ import annotations

from app import config
from ..rule_engine import RuleBasedDetector


def build() -> RuleBasedDetector:
    return RuleBasedDetector(
        name="pii_rule_based",
        kind="pii",
        yaml_path=config.RULES_DIR / "pii_ko.yaml",
    )
