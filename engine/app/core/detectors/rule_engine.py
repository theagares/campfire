"""
app/core/detectors/rule_engine.py
YAML 룰 테이블을 컴파일해 정규식 매칭으로 Detection 을 생성하는 공용 엔진.
PII/인젝션 rule_based detector 가 공유한다 (PLAN §5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from .base import ChunkMeta, Detection

# ── 추가 검증기 (validator) ───────────────────────────────────────────────────


def _rrn_checksum(text: str) -> bool:
    """주민등록번호 체크섬 검증. 하이픈/공백 제거 후 13자리 가중합."""
    digits = re.sub(r"[-\s]", "", text)
    if len(digits) != 13 or not digits.isdigit():
        return False
    weights = [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]
    total = sum(int(d) * w for d, w in zip(digits[:12], weights))
    check = (11 - (total % 11)) % 10
    return check == int(digits[12])


def _luhn(text: str) -> bool:
    """카드번호 Luhn 검증."""
    digits = [int(c) for c in re.sub(r"[-\s]", "", text) if c.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


VALIDATORS: dict[str, Callable[[str], bool]] = {
    "rrn_checksum": _rrn_checksum,
    "luhn": _luhn,
}

_FLAG_MAP = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "VERBOSE": re.VERBOSE,
    "UNICODE": re.UNICODE,
}


@dataclass
class CompiledRule:
    type: str
    regex: re.Pattern
    group: int
    confidence: float
    validator: Callable[[str], bool] | None


def compile_rules(yaml_path: Path) -> list[CompiledRule]:
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    compiled: list[CompiledRule] = []
    for raw in data.get("rules", []):
        flags = 0
        for name in raw.get("flags", []) or []:
            flags |= _FLAG_MAP.get(str(name).upper(), 0)
        compiled.append(
            CompiledRule(
                type=raw["type"],
                regex=re.compile(raw["pattern"], flags),
                group=int(raw.get("group", 0)),
                confidence=float(raw.get("confidence", 0.5)),
                validator=VALIDATORS.get(raw.get("validator", "")),
            )
        )
    return compiled


def run_rules(rules: list[CompiledRule], text: str, source: str = "rule") -> list[Detection]:
    """모든 룰을 돌려 Detection 리스트를 만든다. 겹침 해소는 호출측(pipeline)에서."""
    out: list[Detection] = []
    for rule in rules:
        for m in rule.regex.finditer(text):
            try:
                start, end = m.span(rule.group)
            except (IndexError, re.error):
                start, end = m.span(0)
            if start < 0 or end <= start:
                continue
            matched = text[start:end]
            if rule.validator is not None and not rule.validator(m.group(0)):
                continue
            out.append(
                Detection(
                    type=rule.type,
                    start=start,
                    end=end,
                    text=matched,
                    confidence=rule.confidence,
                    source=source,
                )
            )
    return out


class RuleBasedDetector:
    """YAML 룰 기반 Detector (PLAN §5 Detector Protocol 구현)."""

    def __init__(self, name: str, kind: str, yaml_path: Path):
        self.name = name
        self.kind = kind  # "pii" | "injection"
        self._rules = compile_rules(yaml_path)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    async def detect(self, text: str, *, meta: ChunkMeta | None = None) -> list[Detection]:
        return run_rules(self._rules, text, source="rule")
