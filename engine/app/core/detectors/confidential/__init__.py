"""
app/core/detectors/confidential/__init__.py
기밀 탐지 — 회사 KG 사전 매칭 → 리트리버 → Solar Pro 4 판정 → 문장 단위 CONFIDENTIAL 구간.

  1. kg.py       KG 엔티티 이름이 나오는 위치 → 주어·목적어가 둘 다 나온 트리플(후보)
  2. retriever   문장 단위 창마다(인코더 길이 제한) 후보 쌍·술어의 여유값(s − s_TH)과 문장별 주의
  3. judge       사실이 걸린 창마다(앞뒤 문장 하나씩 겹쳐) 그 창에서 CONFIDENTIAL_DELTA 를 넘은 사실 +
                 PII·인젝션을 가린 문장 → Solar 로 "어느 문장이 진술했고 기밀인가" (창마다 1회, 동시 호출 상한)
  4. 고른 문장 전체를 CONFIDENTIAL 로 — 안쪽 PII·사용자 지정 구간은 따로 남아 자기 토글을 유지한다.
     기밀로 판정된 사실은 같은 묶음 안에서 주어·목적어가 함께 나오는 문장과, 줄바꿈에 쪼개진 바로 옆 문장도 가린다

실패 처리: 리트리버를 못 쓰거나(가중치 없음·오류) Solar 를 못 쓰면(키 없음·오류·해석 불가) 근거
문장을 낮은 신뢰도로 가린다 — 보안 게이트웨이라 막는 쪽. 리트리버 없이 Solar 로 보내지 않는다: 그러면
문서가 말하지도 않은 KG 사실까지 문서와 함께 나간다.
계획: 문서/리트리버/기밀탐지_계획.md
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import Any, Awaitable, Callable

from app import config
from app.core import demo_trace, model_status
from app.core.masker.redact_map import build_redaction

from ..base import CONFIDENTIAL, INJECTION_TYPES, ORGANIZATION, PII_TYPES, Detection
from . import judge, kg, retriever

Emit = Callable[[dict], Awaitable[None]]

MAX_EVIDENCE = 3   # Solar 를 못 쓸 때 사실 하나에 가리는 근거 문장 수
CONTEXT_SENTENCES = 1  # Solar 묶음 앞뒤로 더 붙이는 문장 수 — 창 경계에 걸린 생략 주어·줄바꿈에 쪼개진 문장을 같이 보게
SOLAR_CONCURRENCY = 3  # 문서 하나의 Solar 동시 호출 상한 (Upstage 429)
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


def _spo(t: kg.Triple) -> list[str]:
    return [t.s, t.p, t.o]


async def _score_window(
    text: str, sents, idxs, mentions, cands, use_retriever: bool, scores: dict | None = None,
) -> tuple[list[tuple[kg.Triple, list[int]]], bool]:
    """창 하나에서 리트리버를 통과한 (사실, 근거 문장 번호들) 과, 리트리버로 실제 걸렀는지.
    scores 를 주면 걸러진 것까지 사실별 여유값(창 여럿이면 최댓값, 못 쟀으면 None)을 채운다 (시연 추적용)."""
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
            m = margins[j] if results is not None and margins is not None else None
            if scores is not None:
                t, prev = kg.Triple(s, p, o), scores.get(kg.Triple(s, p, o))
                scores[t] = m if prev is None else (prev if m is None else max(prev, m))
            if m is not None and m <= config.CONFIDENTIAL_DELTA:
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
    text: str,
    masked_items: list[Detection],
    emit: Emit | None = None,
    forced=None,
    *,
    pii_keep: list[dict[str, Any]] | None = None,
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
    trace = demo_trace.start()  # 시연 추적 — 꺼져 있으면 None 이고 아래 publish 는 아무것도 안 한다
    if not cands and trace is None:
        return []

    sents = split_sentences(text)
    # KG 에 있는 회사·기관 이름은 Solar 에 그대로 — 기관명(ORGANIZATION)까지 가리면 사실이 "([기관명 마스킹], 협력사,
    # [기관명 마스킹])" 이 되어 판단할 수 없었다(실측). 개인정보(이름·연락처 등)는 그대로 가린다
    kg_spans = [sp for sps in mentions.values() for sp in sps]
    for_solar = [it for it in masked_items if not (it.get("type") == ORGANIZATION and any(
        _overlaps((int(it["start"]), int(it["end"])), sp) for sp in kg_spans))]

    def no_forced(s: str) -> str:  # 사용자 지정 단어는 어떤 경로로도 Solar 에 원문으로 가지 않는다
        return build_redaction(s, forced.find(s)).text if forced is not None else s

    def solar_sentences() -> list[str]:
        return [no_forced(_redacted(text, sp, for_solar)) for sp in sents]

    def visible_pii_keep() -> list[dict[str, Any]]:
        """Solar가 보는 문장 안에 실제로 남은 MLP KEEP만 시연 정보로 만든다."""
        visible: list[dict[str, Any]] = []
        covers = [(int(it["start"]), int(it["end"])) for it in for_solar]
        seen: set[tuple[int, int, str]] = set()
        for item in pii_keep or []:
            start, end = int(item.get("start", -1)), int(item.get("end", -1))
            pii_type = str(item.get("type", "OTHER_PII"))
            key = (start, end, pii_type)
            if start < 0 or end <= start or end > len(text) or key in seen:
                continue
            seen.add(key)
            if any(_overlaps((start, end), cover) for cover in covers):
                continue  # 다른 창의 MASK·강제 규칙·인젝션에 실제로 가려진 후보
            for sentence, (a, b) in enumerate(sents):
                if a <= start and end <= b:
                    local_masks = [
                        {
                            "start": max(int(mask["start"]), a) - a,
                            "end": min(int(mask["end"]), b) - a,
                            "type": mask.get("type", "OTHER_PII"),
                            "confidence": mask.get("confidence", 1.0),
                        }
                        for mask in for_solar
                        if int(mask["start"]) < b and a < int(mask["end"])
                    ]
                    redaction = build_redaction(text[a:b], local_masks)
                    local_start, local_end = start - a, end - a
                    display_span = next((
                        (
                            masked_start + local_start - original_start,
                            masked_start + local_end - original_start,
                        )
                        for masked_start, _masked_end, original_start, original_end, is_mask
                        in redaction.segments
                        if not is_mask and original_start <= local_start and local_end <= original_end
                    ), None)
                    if display_span is None:
                        break
                    visible.append({
                        "sentence": sentence,
                        "text": text[start:end],
                        "type": pii_type,
                        "maskProbability": float(item.get("maskProbability", 0.0)),
                        "start": display_span[0],
                        "end": display_span[1],
                    })
                    break
        return visible

    if trace is not None:  # 문장은 Solar 가 보는 것(가린 것)만 싣는다
        demo_trace.publish(trace, "start", sentences=solar_sentences(), entities=sorted(mentions),
                           candidates=[_spo(t) for t in cands], delta=config.CONFIDENTIAL_DELTA,
                           piiKeep=visible_pii_keep(),
                           masked=dict(Counter("pii" if it.get("type") in PII_TYPES else "injection"
                                               if it.get("type") in INJECTION_TYPES else "other" for it in masked_items)))
    if not cands:
        demo_trace.publish(trace, "done", flagged=[])
        return []
    if emit:
        await emit({"type": "step", "step": 3, "label": f"기밀 탐지 중 (KG 후보 {len(cands)}개)..."})

    hits: dict[kg.Triple, set[int]] = {}  # 통과한 사실 → 근거 문장 (시연 추적용, 창 전체 합)
    windows: list[tuple[list[int], dict[kg.Triple, set[int]]]] = []  # 창 → 그 창의 후보 사실 → 그 창에서 통과했을 때의 근거
    scores: dict[kg.Triple, float | None] | None = {} if trace is not None else None
    use_retriever = model_status.confidential_ready()
    all_scored = True
    for idxs in _windows(sents, config.CONFIDENTIAL_WINDOW_CHARS):  # 리트리버는 요청을 어차피 하나씩 처리
        window_hits, scored = await _score_window(text, sents, idxs, mentions, cands, use_retriever, scores)
        if not scored:
            # 한 번 못 뜬 리트리버를 창마다 다시 띄우면(창마다 기동 시간 상한까지 대기) 문서 하나에 수 분이 걸린다
            use_retriever = all_scored = False
        ws, we = sents[idxs[0]][0], sents[idxs[-1]][1]
        here = {ent for ent, spans in mentions.items() if any(ws <= a and b <= we for a, b in spans)}
        win: dict[kg.Triple, set[int]] = {t: set() for t in cands if t.s in here and t.o in here}
        for t, ev in window_hits:
            hits.setdefault(t, set()).update(ev)
            win[t].update(ev)
        windows.append((idxs, win))
    # Solar 묶음: 어느 창에서든 리트리버를 통과한 사실을, 그 사실의 주어·목적어가 함께 나오는 모든 창에서 묻는다.
    # 리트리버는 같은 사실을 창마다 다르게 보아(한 창은 통과, 다른 창은 거름) 통과한 창만 물으면 다른 창의
    # 진술을 놓쳤다(실측) — 예전엔 문서 전체를 Solar 에 보내 그 진술도 잡혔다.
    groups: list[tuple[list[int], dict[kg.Triple, set[int]]]] = []  # 문장 번호(앞뒤 겹침 포함), 물을 사실 → 근거
    for idxs, win in windows:
        ask_here = {t: ev for t, ev in win.items() if t in hits}
        if ask_here:
            lo, hi = max(0, idxs[0] - CONTEXT_SENTENCES), min(len(sents), idxs[-1] + 1 + CONTEXT_SENTENCES)
            groups.append((list(range(lo, hi)), ask_here))
    if trace is not None:
        demo_trace.publish(trace, "retriever", scored=all_scored,
                           scores=[[*_spo(t), m] for t, m in scores.items()],
                           passed=[[*_spo(t), sorted(ev)] for t, ev in hits.items()])

    flagged: dict[int, tuple[float, str]] = {}

    def mark(i: int, conf: float, source: str) -> None:
        if i not in flagged or flagged[i][0] < conf:
            flagged[i] = (conf, source)

    if groups:
        # 문서 전체를 한 번에 물으면 Solar 가 사실마다 대표 문장 하나만 적고 나머지를 빠뜨렸다(시연 추적 실측,
        # 55문장 문서에서 "베타 종료(2027년 2월)" 를 진술한 문장 누락). 사실이 걸린 창만, 그 창의 사실만 붙여 따로 묻는다
        # — 관련 없는 창은 나가지 않고, 묶음이 짧아 시간 상한에도 덜 걸린다.
        redacted = solar_sentences()
        verdicts: list[list[tuple[int, int]] | None] = [None] * len(groups)
        infos: list[dict] = [{} for _ in groups]
        if all_scored:  # 리트리버가 거르지 못한 사실은 문서와 함께 내보내지 않는다 — 아래에서 근거 문장을 가린다
            def shown(t: kg.Triple) -> tuple[str, ...]:
                return tuple(no_forced(x) for x in (_display(t.s, mentions, for_solar, text), t.p,
                                                    _display(t.o, mentions, for_solar, text)))

            if trace is not None:
                facts = list(hits)
                demo_trace.publish(trace, "solar", facts=[_spo(t) for t in facts], shown=[list(shown(t)) for t in facts],
                                   calls=len(groups))
            sem = asyncio.Semaphore(SOLAR_CONCURRENCY)

            async def ask(k: int) -> list[tuple[int, int]] | None:
                ids, win = groups[k]
                async with sem:  # [T#] 번호는 그 묶음의 사실 순서 (T1 = list(win)[0])
                    return await judge.judge([redacted[i] for i in ids], [shown(t) for t in win], infos[k])

            verdicts = list(await asyncio.gather(*(ask(k) for k in range(len(groups)))))
        secret: list[tuple[int, kg.Triple]] = []  # (문서 문장 번호, 기밀 사실) — Solar 가 진술했다고 한 것
        public: set[kg.Triple] = set()
        for (ids, win), verdict, info in zip(groups, verdicts, infos):
            facts = list(win)
            if verdict is None:  # 판단 불가 → 이 창의 근거 문장을 낮은 신뢰도로 가린다 (사용자가 패널에서 풀 수 있음)
                for ev in win.values():
                    for i in ev:
                        mark(i, 0.5, "encoder")
                continue
            public.update(facts[t] for t in info.get("public", []))
            for s, t in verdict:
                secret.append((ids[s], facts[t]))
                mark(ids[s], 0.95, "llm")
        # Solar 가 사실마다 문장 하나만 적어도 같은 진술을 놓치지 않게 — KG 표기 위치로 결정적으로 넓힌다
        def has(i: int, ent: str) -> bool:
            return any(_overlaps(sp, sents[i]) for sp in mentions.get(ent, ()))

        asked = {t: {i for ids, win in groups if t in win for i in ids} for _, t in secret}
        for i, t in secret:
            for j in asked[t]:  # 그 사실을 물은 묶음 안에서 주어·목적어가 함께 나오는 문장
                if has(j, t.s) and has(j, t.o):
                    mark(j, 0.9, "llm")
            for j in (i - 1, i + 1):  # 줄바꿈에 쪼개진 진술 — 고른 문장에 없는 쪽이 바로 옆 문장에 있으면
                if 0 <= j < len(sents) and ((not has(i, t.o) and has(j, t.o)) or (not has(i, t.s) and has(j, t.s))):
                    mark(j, 0.9, "llm")
        if trace is not None:
            secret_facts = {t for _, t in secret}
            demo_trace.publish(trace, "verdict", fallback=all(v is None for v in verdicts),
                               cached=all(info.get("cached", False) for info in infos),
                               pairs=[[s, _spo(t)] for s, t in secret],
                               public=[_spo(t) for t in public - secret_facts])
    if trace is not None:
        demo_trace.publish(trace, "done", flagged=[[i, *flagged[i]] for i in sorted(flagged)])

    # 문장 하나 = 기밀 구간 하나. 문장 안의 PII·사용자 지정·인젝션 구간은 따로 남아 그 안에 들어간다 —
    # 화면은 문장을 [기밀 마스킹] 하나로 보이고, 기밀을 풀어도 안쪽은 자기 토글대로 가려진다(masker·확장 mask-segments).
    # 예전엔 그 구간을 빼고 조각으로 내보내 한 문장이 "[기밀] [기관명] [기밀]" 로 쪼개져 보였다.
    covers = [(int(it["start"]), int(it["end"])) for it in masked_items]
    out: list[Detection] = []
    for i in sorted(flagged):
        conf, source = flagged[i]
        if not any(ch.isalnum() for a, b in _subtract(*sents[i], covers) for ch in text[a:b]):
            continue  # 이미 다른 탐지가 전부 가린 문장
        a, b = sents[i]
        sent = text[a:b]
        ls = len(sent) - len(sent.lstrip())
        rs = len(sent) - len(sent.rstrip())
        out.append(Detection(type=CONFIDENTIAL, start=a + ls, end=b - rs, text=sent.strip(),
                             confidence=conf, source=source))
    if emit:
        await emit({"type": "step", "step": 3, "label": f"기밀 탐지 완료 ({len(flagged)}문장)", "done": True})
    return out
