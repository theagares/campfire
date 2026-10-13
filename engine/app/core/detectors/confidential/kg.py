"""
app/core/detectors/confidential/kg.py
회사 KG(주어·관계·목적어 트리플)를 읽고, 문서에서 KG 엔티티 이름이 나오는 위치를 찾는다.

왜 사전 매칭인가:
  - 리트리버는 엔티티 언급 앞뒤에 * 를 붙이고 그 위치 벡터로 엔티티를 표현한다 — KG
    엔티티가 문서 어디에 나오는지 알아야 입력을 만들 수 있다. PII 모델은 회사 고유
    이름(프로젝트·제품 등)을 못 찾는다.
  - KG 트리플 전체를 문서마다 채점할 수 없다. 주어·목적어가 둘 다 나온 트리플만 후보로
    넘기고, KG 이름이 하나도 없는 문서는 여기서 끝난다(모델·Solar 비용 0).
  Aho-Corasick 은 이름 수와 무관하게 문서 길이에 비례하는 시간으로 전부 찾는다.

KG 파일: JSONL, 한 줄에 {"s": 주어, "p": 관계, "o": 목적어, "aliases": {엔티티: [다른 이름]}}.
aliases 는 선택이고 여러 줄에 나눠 적어도 합쳐진다. 기밀 여부 표시는 없다 — 기밀인지는
Solar 가 판단한다. 파일은 매일 바뀔 수 있어 get() 이 수정 시각을 보고 바뀌면 다시 읽는다.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

import ahocorasick

# 1글자 이름은 거의 모든 문서에 걸려 후보만 늘린다.
MIN_NAME_LEN = 2


@dataclass(frozen=True)
class Triple:
    s: str
    p: str
    o: str


class KnowledgeGraph:
    def __init__(self, triples: list[Triple], aliases: dict[str, set[str]]) -> None:
        self.triples = triples
        self.by_subject: dict[str, list[Triple]] = {}
        for t in triples:
            self.by_subject.setdefault(t.s, []).append(t)
        self._auto = ahocorasick.Automaton()
        names: dict[str, str] = {}  # 표기 → 엔티티
        for t in triples:
            for ent in (t.s, t.o):
                for form in {ent} | aliases.get(ent, set()):
                    if len(form) >= MIN_NAME_LEN:
                        names.setdefault(form, ent)  # ponytail: 같은 표기가 두 엔티티면 먼저 나온 쪽 — 동명이인은 구분 못 함
        for form, ent in names.items():
            self._auto.add_word(form, (ent, len(form)))
        self._empty = not names
        if names:
            self._auto.make_automaton()

    def mentions(self, text: str) -> dict[str, list[tuple[int, int]]]:
        """엔티티 → 문서 안 등장 구간들. 겹치면 긴 것, 단어 중간에서 시작하는 매칭은 버린다.

        ponytail: 표기 그대로만 찾는다 — 대소문자·띄어쓰기·오타 변형은 KG aliases 로 채운다.
        """
        if self._empty or not text:
            return {}
        hits: list[tuple[int, int, str]] = []
        for end, (ent, n) in self._auto.iter(text):
            start, stop = end - n + 1, end + 1
            # 앞 글자가 글자/숫자면 다른 단어의 일부("새한빛" 의 "한빛"). 한국어는 뒤에 조사가
            # 붙으므로 끝 경계는 영문·숫자로 끝나는 이름에만 본다("Gaon" vs "Gaonix").
            if start > 0 and text[start - 1].isalnum():
                continue
            if stop < len(text) and text[stop - 1].isascii() and text[stop - 1].isalnum() \
                    and text[stop].isascii() and text[stop].isalnum():
                continue
            hits.append((start, stop, ent))
        out: dict[str, list[tuple[int, int]]] = {}
        last = -1
        for start, stop, ent in sorted(hits, key=lambda h: (h[0], -(h[1] - h[0]))):
            if start < last:
                continue  # 앞의 더 긴 매칭과 겹침
            out.setdefault(ent, []).append((start, stop))
            last = stop
        return out

    def candidates(self, mentions: dict[str, list[tuple[int, int]]]) -> list[Triple]:
        """주어·목적어가 둘 다 문서에 나온 트리플."""
        return [t for s in mentions for t in self.by_subject.get(s, ()) if t.o in mentions and t.o != t.s]


def load(path: Path) -> KnowledgeGraph:
    triples: list[Triple] = []
    aliases: dict[str, set[str]] = {}
    seen: set[Triple] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue  # 모양이 틀린 줄 하나 때문에 검사 전체가 실패하지 않게 그 줄만 버린다
            row_aliases = row.get("aliases")
            for ent, forms in (row_aliases.items() if isinstance(row_aliases, dict) else ()):
                if isinstance(forms, str):
                    forms = [forms]
                if isinstance(forms, list):
                    aliases.setdefault(str(ent), set()).update(str(x) for x in forms)
            if row.get("s") and row.get("p") and row.get("o"):
                t = Triple(str(row["s"]), str(row["p"]), str(row["o"]))
                if t not in seen:
                    seen.add(t)
                    triples.append(t)
    return KnowledgeGraph(triples, aliases)


_lock = threading.Lock()
_cached: tuple[str, float, KnowledgeGraph] | None = None


def get(path: str | Path | None) -> KnowledgeGraph | None:
    """설정된 KG. 파일이 없거나 깨졌으면 None. 수정 시각이 바뀌었을 때만 다시 읽는다."""
    global _cached
    if not path:
        return None
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    with _lock:
        if _cached and _cached[0] == str(p) and _cached[1] == mtime:
            return _cached[2]
        try:
            kg = load(p)
        except Exception:  # noqa: BLE001 - 갱신 중 깨진 파일 — 이전 것 유지
            return _cached[2] if _cached and _cached[0] == str(p) else None
        _cached = (str(p), mtime, kg)
        return kg
