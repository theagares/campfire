"""기밀 탐지 (회사 KG → 리트리버 → Solar) — 실 모델·실 API 없이 가짜로 흐름을 지킨다.

지키는 것:
  - 사전 매칭: 단어 중간 매칭 버림, 겹치면 긴 것, 주어·목적어 둘 다 나온 트리플만 후보
  - Solar 로 원문 PII 가 나가지 않는다 (문장과 사실 문장 모두)
  - 결과 문장에서 PII 구간을 빼서 내보낸다 (PII 는 자기 토글 유지)
  - 리트리버가 거른 사실은 Solar 로 가지 않는다
  - Solar 를 못 쓰면 근거 문장을 낮은 신뢰도로 가린다 (막는 쪽)
  - 파이프라인에서 piiItems 에 CONFIDENTIAL 로 실려 [기밀 마스킹] 된다
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import config
from app.core import model_status
from app.core.detectors import confidential, registry
from app.core.detectors.confidential import judge, kg, retriever

DOC = "가온연구소는 김하늘이 세운 연구소다. 오로라 출시는 2027년 3월로 정했다. 본사는 서울에 있다."
NAME = "김하늘"
PII = [{"type": "PERSON_NAME", "start": DOC.index(NAME), "end": DOC.index(NAME) + len(NAME),
        "text": NAME, "confidence": 0.9, "source": "encoder"}]
KG_ROWS = [
    {"s": "가온연구소", "p": "설립자", "o": "김하늘"},
    {"s": "오로라", "p": "출시 시기", "o": "2027년 3월", "aliases": {"오로라": ["오로라 프로젝트"]}},
    {"s": "가온연구소", "p": "본사 위치", "o": "서울"},
    {"s": "가온연구소", "p": "주력 제품", "o": "베가"},  # 목적어가 문서에 없음 → 후보 아님
]


class _FakeRetriever:
    """쌍마다 술어별 여유값을 정해 둔다. 기본은 통과(+2)."""

    def __init__(self, margins: dict[tuple[str, str], float] | None = None) -> None:
        self.margins = margins or {}
        self.requests: list[dict] = []

    async def score(self, req: dict) -> list[dict]:
        self.requests.append(req)
        out = []
        for _h, _t, pidx in req["pairs"]:
            n = len(req["sentences"])
            out.append({"margins": [self.margins.get(req["predicates"][p], 2.0) for p in pidx],
                        "sent_att": [1.0 / n] * n})
        return out


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _SpySolar:
    """보낸 본문을 붙잡고, 정해진 사실(술어 이름) 이 있는 [T#] 와 그 사실을 진술한 첫 [S#] 를 기밀로 답한다."""

    def __init__(self, secret_predicates: set[str], fail: bool = False) -> None:
        self.secret = secret_predicates
        self.fail = fail
        self.sent: list[str] = []

    async def post(self, _url, headers=None, json=None):  # noqa: A002
        msg = json["messages"][1]["content"]
        self.sent.append(msg)
        if self.fail:
            raise RuntimeError("network down")
        sents = [ln for ln in msg.splitlines() if ln.startswith("[S")]
        facts = [ln for ln in msg.splitlines() if ln.startswith("[T")]
        hits = []
        for ti, f in enumerate(facts, 1):
            if any(p in f for p in self.secret):
                obj = f.split(", ")[-1].rstrip(")")
                si = next((k for k, s in enumerate(sents, 1) if obj in s), 1)
                hits.append({"s": si, "t": ti})
        return _FakeResponse(__import__("json").dumps({"기밀": hits}, ensure_ascii=False))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "kg.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in KG_ROWS), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIDENTIAL_ENABLED", True)
    monkeypatch.setattr(config, "KG_PATH", str(path))
    monkeypatch.setattr(config, "UPSTAGE_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(model_status, "confidential_ready", lambda: True)
    judge._cache.clear()

    def install(fake_retriever, spy):
        monkeypatch.setattr(retriever, "get", lambda: fake_retriever)
        monkeypatch.setattr(judge, "_client", spy)

    return install


def test_사전_매칭_경계와_후보():
    graph = kg.KnowledgeGraph(
        [kg.Triple("한빛", "협력사", "가온"), kg.Triple("가온연구소", "설립자", "김하늘"), kg.Triple("Gaon", "x", "김하늘")],
        {"가온연구소": {"가온연"}},
    )
    doc = "새한빛과 가온연구소는 김하늘과 Gaonix, 가온연 그리고 Gaon 이다."
    m = graph.mentions(doc)
    assert "한빛" not in m                                   # 단어 중간 ("새한빛")
    assert "가온" not in m                                   # "가온연구소" 안의 "가온" 은 긴 매칭에 먹힘
    assert m["가온연구소"] == [(doc.index("가온연구소"), doc.index("가온연구소") + 5), (doc.index("가온연 "), doc.index("가온연 ") + 3)]
    assert m["Gaon"] == [(doc.index("Gaon "), doc.index("Gaon ") + 4)]  # "Gaonix" 는 아님
    cands = {(t.s, t.p, t.o) for t in graph.candidates(m)}
    assert cands == {("가온연구소", "설립자", "김하늘"), ("Gaon", "x", "김하늘")}


def test_문장_분리와_구간_빼기():
    text = "첫 문장이다. 둘째 문장?\n셋째"
    assert [text[a:b] for a, b in confidential.split_sentences(text)] == ["첫 문장이다.", "둘째 문장?", "셋째"]
    assert confidential._subtract(0, 10, [(2, 4), (3, 6), (8, 12)]) == [(0, 2), (6, 8)]


def test_솔라로_PII_가_나가지_않고_결과에서_PII_구간을_뺀다(setup):
    fake, spy = _FakeRetriever(), _SpySolar({"설립자", "출시 시기"})
    setup(fake, spy)
    items = asyncio.run(confidential.detect(DOC, PII))

    assert spy.sent, "Solar 를 부르지 않았다"
    body = "\n".join(spy.sent)
    assert NAME not in body                     # 문장에서도, 사실 문장에서도
    assert "[이름 마스킹]" in body

    found = {DOC[it["start"]:it["end"]] for it in items}
    assert all(it["type"] == "CONFIDENTIAL" and it["source"] == "llm" for it in items)
    assert "오로라 출시는 2027년 3월로 정했다." in found
    assert not any(NAME in s for s in found)    # 첫 문장은 이름을 빼고 조각으로
    assert "가온연구소는" in found and "이 세운 연구소다." in found
    assert not any("서울" in s for s in found)   # 공개 사실(본사 위치) 은 Solar 가 기밀 아니라고 함


def test_리트리버가_거른_사실은_솔라로_가지_않는다(setup):
    fake = _FakeRetriever({"설립자": -5.0, "출시 시기": -5.0, "본사 위치": -5.0})
    spy = _SpySolar({"설립자"})
    setup(fake, spy)
    items = asyncio.run(confidential.detect(DOC, PII))
    assert fake.requests and not spy.sent and items == []


def test_솔라를_못_쓰면_근거_문장을_낮은_신뢰도로_가린다(setup):
    fake, spy = _FakeRetriever({"설립자": -5.0, "본사 위치": -5.0}), _SpySolar(set(), fail=True)
    setup(fake, spy)
    items = asyncio.run(confidential.detect(DOC, PII))
    assert items and all(it["confidence"] == 0.5 and it["source"] == "encoder" for it in items)
    assert any("2027년 3월" in it["text"] for it in items)


def test_꺼져_있거나_KG_가_없으면_아무것도_안_한다(setup, monkeypatch):
    fake, spy = _FakeRetriever(), _SpySolar({"설립자"})
    setup(fake, spy)
    monkeypatch.setattr(config, "CONFIDENTIAL_ENABLED", False)
    assert asyncio.run(confidential.detect(DOC, PII)) == []
    monkeypatch.setattr(config, "CONFIDENTIAL_ENABLED", True)
    monkeypatch.setattr(config, "KG_PATH", "")
    assert asyncio.run(confidential.detect(DOC, PII)) == []
    assert not fake.requests and not spy.sent


class _StubDetector:
    def __init__(self, items):
        self.items = items

    async def detect(self, text, *, meta=None):
        return [dict(it) for it in self.items if it["start"] >= meta["offset"]]


def test_파이프라인에서_piiItems_에_실려_마스킹된다(setup, monkeypatch):
    fake, spy = _FakeRetriever(), _SpySolar({"출시 시기"})
    setup(fake, spy)
    monkeypatch.setattr(model_status, "all_ready", lambda: True)
    monkeypatch.setattr(registry, "get_pii_detector", lambda: _StubDetector(PII))
    monkeypatch.setattr(registry, "get_injection_detector", lambda: _StubDetector([]))
    from app.core.pipeline.orchestrator import run_pipeline

    result = asyncio.run(run_pipeline(text=DOC))
    types = [it["type"] for it in result["piiItems"]]
    assert "CONFIDENTIAL" in types and "PERSON_NAME" in types
    assert "[기밀 마스킹]" in result["maskedText"]
    assert "2027년 3월" not in result["maskedText"]
    assert "[이름 마스킹]" in result["maskedText"]
