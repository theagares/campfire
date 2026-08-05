"""
app/core/masker/redact_map.py
외부 API 로 텍스트를 보내기 전에 PII 를 가리고, 돌아온 좌표를 원문 좌표로 되돌린다.

왜 필요한가: 인젝션 2차 위치특정은 Upstage Solar(외부 API)에 청크 텍스트를 그대로
보냈다. 로컬 우선 보안 게이트웨이가 정작 검사 과정에서 원문 PII 를 밖으로 내보내는
셈이라, 보내기 전에 PII 를 자리표시자로 바꾼다.

그런데 자리표시자는 원본과 길이가 다르다("홍길동" 3자 → "[이름 마스킹]" 8자).
Solar 는 자기가 받은(=마스킹된) 문자열 기준으로 위치를 알려주므로, 그 좌표를 그대로
쓰면 인젝션 구간이 원문에서 엉뚱한 자리를 가리킨다. 그래서 마스킹과 동시에
구간 대응표를 만들어 두고 돌아온 좌표를 원문 좌표로 되돌린다.
"""

from __future__ import annotations

from typing import Any

from .masker import merge_overlapping, placeholder_for, validate_and_fix

Item = dict[str, Any]


class RedactionMap:
    """마스킹된 텍스트와, 그 좌표를 원문 좌표로 되돌리는 대응표.

    segments 는 (masked_start, masked_end, orig_start, orig_end, is_mask) 의 나열이며
    마스킹 텍스트 전체를 빈틈없이 덮는다.
    """

    __slots__ = ("text", "segments", "redacted_count")

    def __init__(self, text: str, segments: list[tuple[int, int, int, int, bool]], redacted_count: int) -> None:
        self.text = text
        self.segments = segments
        self.redacted_count = redacted_count

    def to_original(self, start: int, end: int) -> tuple[int, int] | None:
        """마스킹 텍스트 기준 [start, end) 를 원문 기준 구간으로 되돌린다.

        자리표시자 안쪽을 가리키는 좌표는 그 PII 구간 전체로 확장한다 — 자리표시자는
        원문에서 길이가 다른 한 덩어리라 그 내부를 더 잘게 나눌 방법이 없다.
        """
        if start >= end or not self.segments:
            return None
        start = max(0, min(start, len(self.text)))
        end = max(0, min(end, len(self.text)))
        if start >= end:
            return None

        orig_start: int | None = None
        orig_end: int | None = None
        for m_start, m_end, o_start, o_end, is_mask in self.segments:
            if orig_start is None and m_start <= start < m_end:
                orig_start = o_start if is_mask else o_start + (start - m_start)
            # end 는 배타적이므로 마지막 문자(end-1)가 속한 구간에서 계산한다.
            if m_start <= end - 1 < m_end:
                orig_end = o_end if is_mask else o_start + (end - m_start)
                break
        if orig_start is None or orig_end is None or orig_start >= orig_end:
            return None
        return orig_start, orig_end


def build_redaction(text: str, pii_items: list[Item] | None) -> RedactionMap:
    """PII 를 자리표시자로 바꾼 텍스트와 대응표를 만든다.

    pii_items 는 이 텍스트 기준(청크 기준) 좌표여야 한다. 겹치는 구간은 masker 와
    같은 규칙으로 병합해, 자리표시자가 서로 겹쳐 좌표가 꼬이는 일을 막는다.
    """
    if not pii_items:
        return RedactionMap(text, [(0, len(text), 0, len(text), False)] if text else [], 0)

    items = merge_overlapping(validate_and_fix(text, list(pii_items)))
    items = sorted(items, key=lambda it: (it["start"], it["end"]))

    parts: list[str] = []
    segments: list[tuple[int, int, int, int, bool]] = []
    cursor = 0   # 원문에서 아직 안 쓴 위치
    out_len = 0  # 지금까지 만든 마스킹 텍스트 길이
    for it in items:
        s, e = int(it["start"]), int(it["end"])
        if s < cursor:  # 병합했는데도 겹치면(방어) 건너뛴다
            continue
        if s > cursor:
            plain = text[cursor:s]
            parts.append(plain)
            segments.append((out_len, out_len + len(plain), cursor, s, False))
            out_len += len(plain)
        ph = placeholder_for(it.get("type", "OTHER_PII"))
        parts.append(ph)
        segments.append((out_len, out_len + len(ph), s, e, True))
        out_len += len(ph)
        cursor = e

    if cursor < len(text):
        tail = text[cursor:]
        parts.append(tail)
        segments.append((out_len, out_len + len(tail), cursor, len(text), False))

    return RedactionMap("".join(parts), segments, len(items))
