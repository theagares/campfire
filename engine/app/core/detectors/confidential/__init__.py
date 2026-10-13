"""
app/core/detectors/confidential/__init__.py
기밀 탐지 — 회사 KG 사전 매칭 → 리트리버 → Solar Pro 4 판정 → 문장 단위 CONFIDENTIAL 구간.

  1. kg.py       KG 엔티티 이름이 나오는 위치 → 주어·목적어가 둘 다 나온 트리플(후보)
  2. retriever   문장 단위 창마다(인코더 길이 제한) 후보 쌍·술어의 여유값(s − s_TH)과 문장별 주의
  3. judge       모든 창에서 CONFIDENTIAL_DELTA 를 넘은 사실 전부 + PII·인젝션을 가린 문서 전체 →
                 Solar 1회로 "어느 문장이 진술했고 기밀인가"
  4. 고른 문장에서 PII·사용자 지정 구간을 뺀 나머지를 CONFIDENTIAL 로 (PII 는 자기 토글 유지)

실패 처리: 리트리버를 못 쓰거나(가중치 없음·오류) Solar 를 못 쓰면(키 없음·오류·해석 불가) 근거
문장을 낮은 신뢰도로 가린다 — 보안 게이트웨이라 막는 쪽. 리트리버 없이 Solar 로 보내지 않는다: 그러면
문서가 말하지도 않은 KG 사실까지 문서와 함께 나간다.
계획: 문서/리트리버/기밀탐지_계획.md
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

from app import config
from app.core import model_status
from app.core.masker.redact_map import build_redaction

from ..base import CONFIDENTIAL, ORGANIZATION, Detection
from . import judge, kg, retriever

Emit = Callable[[dict], Awaitable[None]]

MAX_EVIDENCE = 3   # Solar 를 못 쓸 때 사실 하나에 가리는 근거 문장 수
_SENT_END = re.compile(r"[.!?。！？](?=\s|$)|\n+")


def split_sentences(text: str) -> list[tuple[int, int]]:
    """ponytail: 정규식 — 따옴표 안 마침표·약어·목록에서 틀릴 수 있다. 위치가 어긋나는 사례가 나오면 kss."""
    spans: list[tuple[int, int]] = []

    def add(s: int, e: int) -> None:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if s < e:
            spans.append((s, e))

    start = 0
    for m in _SENT_END.finditer(text):
        add(start, m.end())
        start = m.end()
    add(start, len(text))
    return spans


def _windows(sents: list[tuple[int, int]], limit: int) -> list[list[int]]:
    """문장 번호들을 limit 글자 안쪽으로 묶는다. 한 문장이 더 길면 혼자 한 창.

    ponytail: 창끼리 겹치지 않아 주어·목적어가 다른 창에 있는 사실은 채점되지 않는다 —
    놓치는 사례가 보이면 한 문장씩 겹치게.
    """
    out: list[list[int]] = []
    cur: list[int] = []
    for i, (_, e) in enumerate(sents):
        if cur and e - sents[cur[0]][0] > limit:
            out.append(cur)
            cur = []
        cur.append(i)
    if cur:
        out.append(cur)
    return out


def _subtract(s: int, e: int, covers: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[s, e) 에서 covers 구간을 뺀 조각들."""
    pieces: list[tuple[int, int]] = []
    cur = s
    for cs, ce in sorted(covers):
        if ce <= cur or cs >= e:
            continue
        if cs > cur:
            pieces.append((cur, cs))
        cur = max(cur, ce)
    if cur < e:
        pieces.append((cur, e))
    return pieces


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


async def _score_window(
    text: str, sents, idxs, mentions, cands, use_retriever: bool
) -> tuple[list[tuple[kg.Triple, list[int]]], bool]:
    """창 하나에서 리트리버를 통과한 (사실, 근거 문장 번호들) 과, 리트리버로 실제 걸렀는지."""
    ws, we = sents[idxs[0]][0], sents[idxs[-1]][1]
    ents = {ent: [(a, b) for a, b in spans if ws <= a and b <= we] for ent, spans in mentions.items()}
    ents = {ent: spans for ent, spans in ents.items() if spans}
    win_cands = [t for t in cands if t.s in ents and t.o in ents]
    if not win_cands:
        return [], True
    names = list(ents)
    preds = list(dict.fromkeys(t.p for t in win_cands))
    pair_preds: dict[tuple[str, str], list[str]] = {}
    for t in win_cands:
        pair_preds.setdefault((t.s, t.o), []).append(t.p)
    pairs = list(pair_preds)

    def sent_ids(ent: str) -> set[int]:
        return {i for i in idxs for sp in ents[ent] if _overlaps(sp, sents[i])}

    results: list[dict[str, Any]] | None = None
    if use_retriever:
        req = {
            "text": text[ws:we],
            "entities": [[[a - ws, b - ws] for a, b in ents[n]] for n in names],
            "pairs": [[names.index(s), names.index(o), [preds.index(p) for p in pair_preds[(s, o)]]] for s, o in pairs],
            "predicates": preds,
            "sentences": [[sents[i][0] - ws, sents[i][1] - ws] for i in idxs],
        }
        try:
            results = await retriever.get().score(req)
        except Exception:  # noqa: BLE001 - 리트리버 실패 → 호출부가 Solar 없이 근거 문장을 가린다
            results = None

    out: list[tuple[kg.Triple, list[int]]] = []
    for pi, (s, o) in enumerate(pairs):
        res = results[pi] if results is not None else {"margins": None, "sent_att": None}
        att = res.get("sent_att") or [0.0] * len(idxs)
        rank = sorted(range(len(idxs)), key=lambda k: -att[k])
        both = sent_ids(s) & sent_ids(o)
        either = sent_ids(s) | sent_ids(o)
        ev = [idxs[k] for k in rank if idxs[k] in both] or [idxs[k] for k in rank if idxs[k] in either]
        ev = ev[:MAX_EVIDENCE]
        if res.get("sent_att") and idxs[rank[0]] not in ev:
            ev = ev[: MAX_EVIDENCE - 1] + [idxs[rank[0]]]  # 주의가 가장 큰 문장은 늘 포함
        for j, p in enumerate(pair_preds[(s, o)]):
            margins = res.get("margins")
            if results is not None and margins is not None and margins[j] <= config.CONFIDENTIAL_DELTA:
                continue
            out.append((kg.Triple(s, p, o), sorted(ev)))
    return out, results is not None


def _redacted(text: str, span: tuple[int, int], masked_items: list[Detection]) -> str:
    a, b = span
    local = [{"start": max(int(it["start"]), a) - a, "end": min(int(it["end"]), b) - a,
              "type": it.get("type", "OTHER_PII"), "confidence": it.get("confidence", 1.0)}
             for it in masked_items if int(it["start"]) < b and a < int(it["end"])]
    return build_redaction(text[a:b], local).text


def _display(ent: str, mentions, masked_items: list[Detection], text: str) -> str:
    """사실 문장 속 엔티티 — KG 정식 이름이 아니라 문서에 실제로 나온 표기를, 문장과 똑같이 가려서.

    정식 이름을 쓰면 문서에는 별칭("AR-7")만 있어도 정식 이름("오로라" — 사용자 지정 단어일 수도,
    사람 실명일 수도 있다)이 Solar 로 나간다. 문서 표기는 이미 문장과 같은 가림을 거친다.
    """
    return _redacted(text, mentions[ent][0], masked_items)


async def detect(
    text: str, masked_items: list[Detection], emit: Emit | None = None, forced=None,
) -> list[Detection]:
    """masked_items: 이미 가려질 PII·사용자 지정·인젝션 구간 (원문 좌표) — Solar 로 보내기 전 가리고, 결과에서 뺀다.
    forced: 이 요청의 사용자 지정 마스킹 스냅샷 — 문서에 없는 KG 술어 같은 사실 문장 속 등록어도 가린다."""
    if not config.CONFIDENTIAL_ENABLED or not text or not text.strip():
        return []
    graph = await asyncio.to_thread(kg.get, config.KG_PATH)
    if graph is None:
        return []
    mentions = graph.mentions(text)
    cands = graph.candidates(mentions)
    if not cands:
        return []
    if emit:
        await emit({"type": "step", "step": 3, "label": f"기밀 탐지 중 (KG 후보 {len(cands)}개)..."})

    sents = split_sentences(text)
    hits: dict[kg.Triple, set[int]] = {}  # 통과한 사실 → 근거 문장 (Solar 를 못 쓸 때만 쓴다)
    use_retriever = model_status.confidential_ready()
    all_scored = True
    for idxs in _windows(sents, config.CONFIDENTIAL_WINDOW_CHARS):  # 리트리버는 요청을 어차피 하나씩 처리
        window_hits, scored = await _score_window(text, sents, idxs, mentions, cands, use_retriever)
        if not scored:
            # 한 번 못 뜬 리트리버를 창마다 다시 띄우면(창마다 기동 시간 상한까지 대기) 문서 하나에 수 분이 걸린다
            use_retriever = all_scored = False
        for t, ev in window_hits:
            hits.setdefault(t, set()).update(ev)

    flagged: dict[int, tuple[float, str]] = {}
    if hits:
        # KG 에 있는 회사·기관 이름은 Solar 에 그대로 — 기관명(ORGANIZATION)까지 가리면 사실이 "([기관명 마스킹], 협력사,
        # [기관명 마스킹])" 이 되어 판단할 수 없었다(실측). 개인정보(이름·연락처 등)는 그대로 가린다
        kg_spans = [sp for sps in mentions.values() for sp in sps]
        for_solar = [it for it in masked_items if not (it.get("type") == ORGANIZATION and any(
            _overlaps((int(it["start"]), int(it["end"])), sp) for sp in kg_spans))]

        def no_forced(s: str) -> str:  # 사용자 지정 단어는 어떤 경로로도 Solar 에 원문으로 가지 않는다
            return build_redaction(s, forced.find(s)).text if forced is not None else s

        verdict = None
        if all_scored:  # 리트리버가 거르지 못한 사실은 문서와 함께 내보내지 않는다 — 아래에서 근거 문장을 가린다
            # ponytail: 문서 전체를 Solar 1회 — 컨텍스트·타임아웃을 넘는 긴 문서가 나오면 사실이 걸린 문장 주변만 보낸다
            verdict = await judge.judge(
                [no_forced(_redacted(text, sp, for_solar)) for sp in sents],
                [tuple(no_forced(x) for x in (_display(t.s, mentions, for_solar, text), t.p,
                                               _display(t.o, mentions, for_solar, text))) for t in hits],
            )
        if verdict is None:  # 판단 불가 → 근거 문장을 낮은 신뢰도로 가린다 (사용자가 패널에서 풀 수 있음)
            flagged = {i: (0.5, "encoder") for ev in hits.values() for i in ev}
        else:
            flagged = {s: (0.95, "llm") for s, _ in verdict}

    covers = [(int(it["start"]), int(it["end"])) for it in masked_items]
    out: list[Detection] = []
    for i in sorted(flagged):
        conf, source = flagged[i]
        for a, b in _subtract(*sents[i], covers):
            piece = text[a:b]
            if not any(ch.isalnum() for ch in piece):
                continue
            ls = len(piece) - len(piece.lstrip())
            rs = len(piece) - len(piece.rstrip())
            out.append(Detection(type=CONFIDENTIAL, start=a + ls, end=b - rs, text=piece.strip(),
                                 confidence=conf, source=source))
    if emit:
        await emit({"type": "step", "step": 3, "label": f"기밀 탐지 완료 ({len(flagged)}문장)", "done": True})
    return out
