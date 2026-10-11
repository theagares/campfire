"""
app/core/detectors/confidential/judge.py
Solar Pro 4 기밀 판정 — 리트리버가 "문서가 말하는 것 같다"고 한 KG 사실과 근거 문장을 주고,
① 그 문장이 그 사실을 실제로 진술하는가 ② 그 사실이 회사 기밀인가 를 함께 묻는다.
KG 에는 공개 사실도 섞여 있어(기밀 표시 없음) 기밀 여부는 Solar 가 판단한다.

나가는 것: PII·사용자 지정 단어를 가린 근거 문장 + 사실 문장(엔티티가 PII 면 같은 자리표시자).
호출 모양은 인젝션 위치 특정(injection/llm_mcp._localize_with_solar)과 같다 — 결과 캐시,
세 상태 반환:
  None : 묻지 못했거나 답을 해석 못 함 → 호출부가 막는 쪽(fail-closed)으로 처리
  []   : 진술된 기밀이 없다고 명확히 답함
  [..] : (문장 번호, 사실 번호) — 0부터
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict

import httpx

from app import config

SYSTEM_PROMPT = (
    "너는 회사 문서의 기밀 판정기다.\n"
    "[문장]은 회사 문서에서 뽑은 문장들이다. 개인정보 등은 [이름 마스킹] 처럼 가려져 있다.\n"
    "[사실]은 회사 지식그래프의 (주어, 관계, 목적어) 사실들이다. 공개된 사실과 기밀 사실이 섞여 있다.\n"
    "각 사실마다 판단하라.\n"
    "1) 어느 문장이 그 사실을 실제로 진술하는가. 이름만 나오거나 다른 내용을 말하면 진술이 아니다.\n"
    "2) 그 사실이 회사 밖으로 나가면 안 되는 기밀인가.\n"
    "   기밀 예: 출시·발표 전 제품과 사업 계획, 예산·매출·원가·단가 등 미공개 수치, 계약·거래 조건, "
    "내부 조직·인사, 미공개 기술·연구, 고객·협력사 정보.\n"
    "   기밀 아님 예: 회사 소재지·설립 연도처럼 이미 공개되어 누구나 아는 사실.\n"
    "   애매하면 기밀로 본다.\n"
    '진술되었고 기밀인 경우만 골라 JSON 하나로만 답하라: {"기밀": [{"s": 문장번호, "t": 사실번호}]}\n'
    '없으면 {"기밀": []}'
)
_PROMPT_CACHE_KEY = "campfire-confidential-judge-v1"  # 시스템 프롬프트 개정 시 올린다
_CACHE_MAX = 256
_cache: OrderedDict[str, list[tuple[int, int]]] = OrderedDict()
_client: httpx.AsyncClient | None = None


def user_message(sentences: list[str], facts: list[tuple[str, str, str]]) -> str:
    lines = ["[문장]"] + [f"[S{i + 1}] {s}" for i, s in enumerate(sentences)]
    lines += ["", "[사실]"] + [f"[T{i + 1}] ({s}, {p}, {o})" for i, (s, p, o) in enumerate(facts)]
    return "\n".join(lines)


def parse(content: str, n_sent: int, n_fact: int) -> list[tuple[int, int]] | None:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    items = obj.get("기밀")
    if not isinstance(items, list):
        lists = [v for v in obj.values() if isinstance(v, list)]  # 키 이름을 틀리게 뱉는 경우 (인젝션 쪽 실측)
        if len(lists) != 1:
            return None
        items = lists[0]
    out: list[tuple[int, int]] = []
    for it in items:
        try:
            s, t = int(it["s"]), int(it["t"])
        except (KeyError, TypeError, ValueError):
            return None
        if 1 <= s <= n_sent and 1 <= t <= n_fact:
            out.append((s - 1, t - 1))
    if items and not out:
        return None  # 번호를 하나도 못 맞췄다 — "없다" 가 아니라 "못 맞췄다"
    return out


async def judge(sentences: list[str], facts: list[tuple[str, str, str]]) -> list[tuple[int, int]] | None:
    global _client
    if not config.UPSTAGE_API_KEY or not sentences or not facts:
        return None
    message = user_message(sentences, facts)
    key = hashlib.sha256(message.encode("utf-8")).hexdigest()  # 실제로 보내는 것 기준
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    try:
        if _client is None:
            _client = httpx.AsyncClient(timeout=config.UPSTAGE_TIMEOUT_SEC)
        resp = await _client.post(
            config.UPSTAGE_API_BASE,
            headers={"Authorization": f"Bearer {config.UPSTAGE_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": config.UPSTAGE_MODEL,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": message}],
                "temperature": 0,
                "prompt_cache_key": _PROMPT_CACHE_KEY,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001 - 네트워크/API 오류 → 판단 불가
        return None
    result = parse(content, len(sentences), len(facts))
    if result is not None:
        _cache[key] = result
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return result
