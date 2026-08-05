"""
app/core/masker/masker.py
탐지 위치 정보를 받아 텍스트를 마스킹한다.
파이프라인/extension/utils/masker.js 를 파이썬으로 이식:
  - validate_and_fix : 위치 검증·보정(off-by-one, ±5자 탐색, 전체 검색 복구)
  - merge_overlapping: 겹침 병합 (겹치면 더 높은 confidence 유형을 대표로)
  - apply_masking    : 뒤에서부터 치환(앞쪽 인덱스 보존)

PII 는 유형별 라벨([이름 마스킹] 등), 인젝션은 PLAN §4 대로 [인젝션 마스킹] 고정.
"""

from __future__ import annotations

from typing import Any

Item = dict[str, Any]

# PLAN §5 / masker.js TYPE_LABELS (PII 유형 → 한국어 라벨)
TYPE_LABELS: dict[str, str] = {
    "PERSON_NAME": "이름",
    "EMAIL": "이메일",
    "PHONE": "전화번호",
    "ADDRESS": "주소",
    "ID_NUMBER": "신분증번호",
    "CREDIT_CARD": "카드번호",
    "DATE_OF_BIRTH": "생년월일",
    "ORGANIZATION": "기관명",
    "BANK_ACCOUNT": "계좌번호",
    "OTHER_PII": "개인정보",
}

# 인젝션 유형 전체 — 어떤 유형이든 [인젝션 마스킹] 으로 치환 (PLAN §4)
INJECTION_LABEL = "인젝션"

INJECTION_TYPES = {
    "INSTRUCTION_OVERRIDE",
    "ROLE_MANIPULATION",
    "SYSTEM_PROMPT_LEAK",
    "JAILBREAK",
    "HIDDEN_COMMAND",
    "DATA_EXFILTRATION",
    "OTHER_INJECTION",
}


def get_label(item_type: str) -> str:
    if item_type in INJECTION_TYPES:
        return INJECTION_LABEL
    return TYPE_LABELS.get(item_type, "개인정보")


def placeholder_for(item_type: str) -> str:
    return f"[{get_label(item_type)} 마스킹]"


def validate_and_fix(text: str, items: list[Item]) -> list[Item]:
    """위치가 실제 텍스트와 일치하는지 검증·보정 (masker.js validateAndFix 이식).

    항목의 모양 자체가 어긋난 것(dict 이 아니거나 type 이 문자열이 아닌 것)은 여기서
    버린다. 이 함수는 MCP 의 mask_text 를 통해 **외부 입력**(AI 가 채워 보낸 목록)을
    직접 받는 경로에 있어서, 아래 단계들이 item["type"] 을 그대로 인덱싱하면
    KeyError/TypeError 로 500 이 난다.
    """
    valid: list[Item] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            continue
        start = item.get("start")
        end = item.get("end")
        item_text = item.get("text") or ""
        if not isinstance(item_text, str):
            continue

        # 항목에 원문(text)이 없을 수 있다 — MCP 응답은 원문 유출을 막으려고 text 를
        # 빼고 내보낸다(adapters/mcp/tools.py 의 _redact_items). 그 항목을 그대로
        # mask_text 에 되돌려주는 흐름이 정상 경로다.
        #
        # 그때는 내용 대조를 할 수단이 없으니 좌표를 신뢰한다. 이 분기가 없으면 아래
        # 대조가 전부 실패하고 text.find("") 도 -1 이 되어 **항목이 통째로 버려진다** —
        # 마스킹이 하나도 적용되지 않았는데 호출은 성공으로 끝나는 최악의 실패다.
        if not item_text:
            if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
                valid.append(item)
            continue

        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end > len(text)
            or start >= end
        ):
            idx = text.find(item_text) if item_text else -1
            if idx != -1:
                valid.append({**item, "start": idx, "end": idx + len(item_text)})
            continue

        if text[start:end] == item_text:
            valid.append(item)
            continue

        found = False
        for offset in range(-5, 6):
            s = start + offset
            e = end + offset
            if s >= 0 and e <= len(text) and text[s:e] == item_text:
                valid.append({**item, "start": s, "end": e})
                found = True
                break
        if not found:
            idx = text.find(item_text) if item_text else -1
            if idx != -1:
                valid.append({**item, "start": idx, "end": idx + len(item_text)})
    return valid


def merge_overlapping(items: list[Item]) -> list[Item]:
    """겹치는 범위를 병합 (masker.js mergeOverlapping 이식 + 유형 선택 개선).

    겹치면 범위를 합치고, 대표 유형은 confidence 가 높은(동률이면 더 긴) 항목으로
    둔다 — 예: 주민번호(0.98) vs 전화번호가 겹치면 주민번호 유형이 남는다.
    """
    if not items:
        return []
    ordered = sorted(items, key=lambda x: (x["start"], -(x["end"] - x["start"])))
    merged: list[Item] = [dict(ordered[0])]
    for cur in ordered[1:]:
        last = merged[-1]
        if cur["start"] < last["end"]:
            # 겹침 — 더 높은 우선순위(confidence, 길이) 유형을 대표로 채택.
            # confidence 가 None 으로 실려오는 경로가 있다(MCP _redact_items 를 거친
            # 항목, secure_search_files 의 라인 단위 재구성) — or 0.0 으로 받아낸다.
            cur_score = (cur.get("confidence") or 0.0, cur["end"] - cur["start"])
            last_score = (last.get("confidence") or 0.0, last["end"] - last["start"])
            if cur_score > last_score:
                last["type"] = cur["type"]
                last["confidence"] = cur.get("confidence", last.get("confidence", 0.0))
            new_end = max(last["end"], cur["end"])
            last["end"] = new_end
            last["text"] = None  # 병합 후 대표 text 는 apply 시 재산출
        else:
            merged.append(dict(cur))
    return merged


def apply_masking(text: str, items: list[Item]) -> dict[str, Any]:
    """텍스트에 마스킹을 적용 (masker.js applyMasking 이식).

    반환: {"masked_text": str, "applied": [ {type,start,end,placeholder,...} ]}
    (start/end 는 원본 text 기준)
    """
    validated = validate_and_fix(text, items)
    unique = merge_overlapping(validated)

    # 뒤에서부터 처리하면 앞쪽 인덱스가 유지됨
    ordered = sorted(unique, key=lambda x: x["start"], reverse=True)

    result = text
    applied: list[Item] = []
    for item in ordered:
        ph = placeholder_for(item["type"])
        result = result[: item["start"]] + ph + result[item["end"] :]
        applied.append(
            {
                "type": item["type"],
                "start": item["start"],
                "end": item["end"],
                "placeholder": ph,
                "confidence": item.get("confidence"),
                "source": item.get("source"),
            }
        )
    applied.reverse()
    return {"masked_text": result, "applied": applied}
