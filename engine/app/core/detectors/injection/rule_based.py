"""
app/core/detectors/injection/rule_based.py
프롬프트 인젝션 룰베이스 detector (PLAN §5, rules/injection_ko.yaml).

교체 시나리오: llm_mcp.py 를 추가하고 같은 Detection 을 반환하면
설정(registry)에서 injection: llm_mcp 로 전환만 하면 된다.
"""

from __future__ import annotations

from app import config
from ..rule_engine import RuleBasedDetector


def build() -> RuleBasedDetector:
    return RuleBasedDetector(
        name="injection_rule_based",
        kind="injection",
        yaml_path=config.RULES_DIR / "injection_ko.yaml",
    )
