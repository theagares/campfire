"""내부 고정 PII 규칙 — 모델 결과와 상관없이 늘 PII 로 잡는 단어(정규식, 모델 불필요).

유형은 PII 모델이 직함(CV_POSITION)에 쓰는 OTHER_PII 와 같다 — 패널에서 다른 PII 처럼 빨강으로 보이고 [개인정보 마스킹]
으로 가린다. 사용자 지정 강제 마스킹(설정·저장소, forcedMaskItems)과는 따로다.
"""

from __future__ import annotations

import re

from app.core.detectors.base import OTHER_PII, Detection

_TERMS = re.compile("팀장")  # 「팀장님」「OO팀장」처럼 붙어 있어도 이 부분만
_CONFIDENCE = 0.9  # 규칙 개체 — encoder._RULE_CONFIDENCE 와 같다(표시용)


def detect(text: str) -> list[Detection]:
    return [{"type": OTHER_PII, "start": m.start(), "end": m.end(), "text": m.group(), "confidence": _CONFIDENCE,
             "source": "rule"} for m in _TERMS.finditer(text)]


def merge(items: list[Detection], text: str) -> list[Detection]:
    """PII 결과에 고정 규칙을 더한다. 이미 잡힌 구간과 겹치면 하나로 — 안에 들면 더하지 않고, 걸치면 그 항목을 넓힌다."""
    out = [dict(i) for i in items]
    for hit in detect(text):
        over = next((i for i in out if i["start"] < hit["end"] and hit["start"] < i["end"]), None)
        if over is None:
            out.append(hit)
        elif hit["start"] < over["start"] or hit["end"] > over["end"]:
            over["start"], over["end"] = min(over["start"], hit["start"]), max(over["end"], hit["end"])
            over["text"] = text[over["start"]:over["end"]]
    return sorted(out, key=lambda x: (x["start"], x["end"]))
