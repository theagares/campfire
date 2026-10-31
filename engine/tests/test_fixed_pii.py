"""내부 고정 PII 규칙(detectors/pii/fixed.py) — 모델 없이. 확장·프록시·프롬프트·문서가 모두 지나는 orchestrator 의 PII 합치는
자리에서 더해지고, 유형은 OTHER_PII(패널 빨강·[개인정보 마스킹]), 사용자 지정 강제 마스킹과는 따로다."""

from __future__ import annotations

import asyncio
import io

import pytest

from app import config
from app.core import model_status
from app.core.detectors import registry
from app.core.detectors.pii import fixed
from app.core.pipeline import orchestrator
from app.core.pipeline.orchestrator import run_pipeline

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _spans(text: str) -> list[tuple[str, str]]:
    return [(i["type"], text[i["start"]:i["end"]]) for i in fixed.detect(text)]


@pytest.mark.parametrize("text, want", [
    ("김철수 팀장에게 보고", [("OTHER_PII", "팀장")]),
    ("팀장님께 전달", [("OTHER_PII", "팀장")]),
    ("영업팀장 회의", [("OTHER_PII", "팀장")]),
    ("팀장 두 명과 팀장님", [("OTHER_PII", "팀장"), ("OTHER_PII", "팀장")]),
    ("팀 회의", []), ("장 보기", []), ("팀 장", []), ("장팀", []),
])
def test_팀장은_붙어_있어도_그_부분만_잡고_팀_장_단독은_안_잡는다(text, want):
    assert _spans(text) == want


def test_이미_잡힌_PII_와_겹치면_하나로():
    text = "김철수 팀장님"
    model = [{"type": "PERSON_NAME", "start": 0, "end": 3, "text": "김철수", "confidence": 0.95, "source": "encoder"},
             {"type": "OTHER_PII", "start": 4, "end": 7, "text": "팀장님", "confidence": 0.8, "source": "encoder"}]
    assert fixed.merge(model, text) == model, "모델이 이미 덮은 구간에 또 더하면 같은 글이 두 건으로 센다"
    partial = [{"type": "ORGANIZATION", "start": 0, "end": 5, "text": "김철수 팀", "confidence": 0.7, "source": "encoder"}]
    merged = fixed.merge(partial, text)
    assert [(i["type"], i["start"], i["end"], i["text"]) for i in merged] == [("ORGANIZATION", 0, 6, "김철수 팀장")]
    assert partial[0]["end"] == 5, "넘겨받은 목록을 바꾸면 안 된다"


@pytest.fixture
def clean_models(monkeypatch):
    """PII·인젝션 모델이 아무것도 못 찾는 가짜 — 고정 규칙만으로 잡히는지 본다."""
    class Clean:
        async def detect(self, text, *, meta=None):
            return []

    monkeypatch.setattr(model_status, "all_ready", lambda: True)
    monkeypatch.setattr(model_status, "pii_ready", lambda: True)
    monkeypatch.setattr(registry, "get_pii_detector", lambda: Clean())
    monkeypatch.setattr(registry, "get_injection_detector", lambda: Clean())
    monkeypatch.setattr(config, "INJECTION_POLICY", "mask")


def _docx(text: str) -> bytes:
    from docx import Document

    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _pii(result, key="piiItems"):
    return [(i["type"], i["text"]) for i in result[key]]


def test_프롬프트_문서_문서와_온_프롬프트_모두_PII_로_잡혀_빨강_개인정보로_가린다(clean_models):
    prompt = asyncio.run(run_pipeline(text="김철수 팀장에게 보고", file_name="prompt.txt", direct_prompt=True,
                                      scan_injection=False))
    assert _pii(prompt) == [("OTHER_PII", "팀장")]
    assert prompt["maskedText"] == "김철수 [개인정보 마스킹]에게 보고"
    assert prompt["forcedMaskItems"] == [], "사용자 지정 강제 마스킹(다른 색·잠금)으로 넣으면 안 된다"

    txt = asyncio.run(run_pipeline(file_bytes="팀장님 회의록".encode(), mime_type="text/plain", file_name="a.txt"))
    assert _pii(txt) == [("OTHER_PII", "팀장")] and txt["maskedText"] == "[개인정보 마스킹]님 회의록"

    doc = asyncio.run(run_pipeline(file_bytes=_docx("영업팀장 보고서"), mime_type=DOCX, file_name="a.docx"))
    assert _pii(doc) == [("OTHER_PII", "팀장")] and "[개인정보 마스킹]" in doc["maskedText"]

    with_prompt = asyncio.run(run_pipeline(text="본문", file_name="a.txt", user_prompt="팀장님 확인 부탁"))
    assert _pii(with_prompt, "userPromptPiiItems") == [("OTHER_PII", "팀장")]
    assert with_prompt["userPromptMasked"] == "[개인정보 마스킹]님 확인 부탁"

    plain = asyncio.run(run_pipeline(text="팀 회의는 장소를 바꿔", file_name="p.txt", direct_prompt=True,
                                     scan_injection=False))
    assert plain["piiItems"] == [] and plain["maskedText"] == "팀 회의는 장소를 바꿔"


def test_모델이_아직_없어도_가린다(monkeypatch):
    monkeypatch.setattr(model_status, "all_ready", lambda: False)
    result = asyncio.run(run_pipeline(text="김철수 팀장 보고", file_name="p.txt"))
    assert result["scanStatus"] == "models_not_ready"
    assert result["maskedText"] == "김철수 [개인정보 마스킹] 보고"


def test_통합_GPU_worker_경로에서도_더한다(monkeypatch):
    class Pii:
        name, kind = "fake_pii", "pii"

        def prepare_windows(self, text):
            return [(text, 0)]

        def decode_windows(self, text, windows, entity_lists):
            return []

        async def detect(self, text, *, meta=None):
            raise AssertionError("통합 경로가 개별 PII 사이드카를 띄웠다")

    class Injection:
        name, kind = "fake_injection", "injection"

        async def classify_batch_local(self, texts, *, user_prompts=None):
            raise AssertionError("통합 경로가 개별 인젝션 사이드카를 띄웠다")

        async def finalize_batch_local(self, texts, results, *, metas=None):
            return [[] for _ in texts]

    class Worker:
        async def infer_parallel(self, pii_texts, injection_records):
            return [[] for _ in pii_texts], [{"is_injection": False, "scores": {}} for _ in injection_records]

    monkeypatch.setattr(orchestrator.model_status, "all_ready", lambda: True)
    monkeypatch.setattr(orchestrator.registry, "get_pii_detector", lambda: Pii())
    monkeypatch.setattr(orchestrator.registry, "get_injection_detector", lambda: Injection())
    monkeypatch.setattr(orchestrator.combined_gpu, "get_worker", lambda: Worker())
    monkeypatch.setattr(orchestrator.injection_chunker, "split_injection_chunks",
                        lambda value: [{"text": value, "offset": 0}])
    result = asyncio.run(run_pipeline(text="김철수 팀장 보고", user_prompt="팀장님 확인", file_name="c.txt"))
    assert _pii(result) == [("OTHER_PII", "팀장")]
    assert _pii(result, "userPromptPiiItems") == [("OTHER_PII", "팀장")]
