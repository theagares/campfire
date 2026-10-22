"""회사 연락처 풀어 두기(keep) — 표시 규칙과 keep_scorer 의 순수 함수(모델 없이)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from app import config
from app.core import model_status
from app.core.detectors.pii import keep

_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "app" / "models" / "pii_engine" / "runtime"


def _load_scorer():
    spec = importlib.util.spec_from_file_location("_pii_keep_scorer", _RUNTIME_DIR / "keep_scorer.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ks = _load_scorer()


def _item(label, start, end, source="encoder"):
    return {"type": "X", "start": start, "end": end, "text": "", "confidence": 0.9, "source": source, "label": label}


class _FakeProcess:
    def __init__(self, probs):
        self.probs = probs
        self.requests = []
        self.residency = type("R", (), {"touch": lambda self: None})()

    async def _ensure_process(self):
        return None

    async def _rpc(self, request):
        self.requests.append(request)
        return {"probs": self.probs[: len(request["entities"])], "threshold": 0.6}


@pytest.fixture
def keep_on(monkeypatch):
    monkeypatch.setattr(config, "PII_KEEP_ENABLED", True)
    monkeypatch.setattr(model_status, "pii_keep_ready", lambda: True)

    def install(probs):
        fake = _FakeProcess(probs)
        monkeypatch.setattr(keep, "_process", fake)
        return fake
    return install


def test_only_company_contact_labels_are_scored(keep_on):
    fake = keep_on([0.01, 0.9, None])
    items = [_item("QT_PHONE", 0, 12), _item("TMI_EMAIL", 13, 30), _item("LC_ADDRESS", 31, 40),
             _item("QT_MOBILE", 41, 54), _item("PS_NAME", 55, 58), _item("QT_PHONE", 60, 70, source="rule")]
    asyncio.run(keep.annotate("x" * 80, items))
    assert [e["label"] for e in fake.requests[0]["entities"]] == ["QT_PHONE", "TMI_EMAIL", "LC_ADDRESS"]
    assert items[0]["keep"] is True and items[0]["maskProb"] == 0.01
    assert items[1]["keep"] is False  # 임계값 이상 → 가린다
    assert "keep" not in items[2]  # 점수 없음 → 표시 없음(가린다)
    assert all("keep" not in it for it in items[3:])  # 휴대폰·이름·정규식 탐지는 손대지 않는다


def test_off_missing_or_long_text_leaves_items_untouched(keep_on, monkeypatch):
    fake = keep_on([0.01])
    items = [_item("QT_PHONE", 0, 12)]
    monkeypatch.setattr(config, "PII_KEEP_MAX_CHARS", 10)
    asyncio.run(keep.annotate("x" * 11, items))
    monkeypatch.setattr(config, "PII_KEEP_MAX_CHARS", 20000)
    monkeypatch.setattr(model_status, "pii_keep_ready", lambda: False)
    asyncio.run(keep.annotate("x" * 11, items))
    monkeypatch.setattr(model_status, "pii_keep_ready", lambda: True)
    monkeypatch.setattr(config, "PII_KEEP_ENABLED", False)
    asyncio.run(keep.annotate("x" * 11, items))
    assert fake.requests == [] and "keep" not in items[0]


def test_failure_leaves_everything_masked(keep_on):
    fake = keep_on([0.01])

    async def boom(request):
        raise RuntimeError("sidecar died")
    fake._rpc = boom
    items = [_item("QT_PHONE", 0, 12)]
    asyncio.run(keep.annotate("x" * 20, items))
    assert "keep" not in items[0]


def test_split_sentences_and_bio_decode():
    text = "문의: 031-676-4069.\n담당 김민수! 끝"
    assert [text[a:b] for a, b in ks.split_sentences(text)] == ["문의: 031-676-4069.", "담당 김민수!", "끝"]
    B, I = ks.BIO.index, ks.BIO.index
    offsets = [(0, 0), (0, 2), (2, 5), (5, 6), (0, 0)]
    tags = [0, B("B-NAME"), I("I-NAME"), I("I-ADDRESS"), 0]
    # I-ADDRESS 가 다른 종류 뒤에 오면 새 개체로 시작한다(원본 decode_bio 와 같다)
    assert ks._decode_bio(offsets, tags) == [{"begin": 0, "end": 5, "label": "NAME"}, {"begin": 5, "end": 6, "label": "ADDRESS"}]


def test_viterbi_respects_bio_constraints():
    import torch
    start, trans = ks._crf_constraints()
    em = torch.zeros(2, ks.BIO_SIZE)
    em[0, ks.BIO.index("I-EMAIL")] = 5.0  # I- 로 시작하는 경로는 막혀 있다
    em[1, ks.BIO.index("I-EMAIL")] = 5.0
    path = ks._viterbi(em, start, torch.zeros(ks.BIO_SIZE), trans)
    assert ks.BIO[path[0]] != "I-EMAIL"
