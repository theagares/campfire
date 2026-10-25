"""PII 런타임(direct MASK/KEEP 모델 감싸기) — 도메인 최댓값·KEEP 제외·라벨 코드·보정·FP32 고정.

가중치 없이 돌도록 모델 자리에 가짜를 넣는다: 확인하는 건 래퍼의 판정 규칙이다.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import torch

_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "app" / "models" / "pii_engine" / "runtime"


def _load_runtime():
    sys.path.insert(0, str(_RUNTIME_DIR))
    try:
        spec = importlib.util.spec_from_file_location("_pii_runtime_direct", _RUNTIME_DIR / "local_pii_inference.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_RUNTIME_DIR))


rt = _load_runtime()
DOMAINS = ["career_education", "contract", "financial", "legal", "medical", "support"]
TEXT = "김민준은 미르온테크 직원이고 연락처는 010-1234-5678"


class _FakeModel:
    """P1(이름)은 support 도메인만 가리라고 하고, P2(회사)는 모든 도메인이 남겨도 된다고 한다."""

    def tokenize(self, public):
        return {"public": public}

    def encode(self, data, predict=False):
        sent = {row["sent_idx"] for row in data["public"]["sentences"]}
        entities = [
            {"entity_id": "P1", "entity_type": "NAME",
             "mentions": [{"sent_idx": "0", "begin": 0, "end": 3, "form": "김민준"},
                          {"sent_idx": "2", "begin": 0, "end": 3, "form": "김민준"}]},
            {"entity_id": "P2", "entity_type": "WORKPLACE",
             "mentions": [{"sent_idx": "0", "begin": 5, "end": 10, "form": "미르온테크"}]},
        ]
        for e in entities:
            e["mentions"] = [m for m in e["mentions"] if m["sent_idx"] in sent]
        return torch.zeros(1), entities, None

    def contextual(self, hidden, data, entities, domain):
        return torch.tensor([3.0 if domain == "support" else -5.0, -5.0]), []


def _detector():
    det = rt.PIIDetector.__new__(rt.PIIDetector)
    det.model, det.domains, det.threshold = _FakeModel(), DOMAINS, 0.1
    det.codes = {"NAME": "PS_NAME", "WORKPLACE": "OG_WORKPLACE"}
    return det


def test_한_도메인이라도_가리라고_하면_가리고_모두_남기라면_뺀다():
    first, blank, again = _detector().predict([TEXT, "   ", "김민준 확인"])
    labels = [(e["label"], TEXT[e["begin"]:e["end"]]) for e in first]
    assert ("PS_NAME", "김민준") in labels
    assert not any(label == "OG_WORKPLACE" for label, _ in labels)  # KEEP
    assert next(e for e in first if e["label"] == "PS_NAME")["score"] > 0.9
    assert blank == []
    assert [e["form"] for e in again] == ["김민준"]  # 다른 창의 같은 개체도 같은 판정


def test_정규식_보정은_그대로_더하고_score_없이_항상_가린다():
    first = _detector().predict([TEXT])[0]
    phone = next(e for e in first if e["label"] == "QT_MOBILE")
    assert phone["form"] == "010-1234-5678" and "score" not in phone


def test_모델이_자른_이메일은_정규식이_전체를_잡는다():
    text = "문의는 doyoon.kim@hecto-demo.kr입니다"
    cut = {"form": "kim@hecto-demo.kr", "label": "TMI_EMAIL", "begin": 11, "end": 28, "score": 0.9}
    out = rt.regex_postprocess(text, [cut])
    assert [e["form"] for e in out] == ["doyoon.kim@hecto-demo.kr"]


def test_dtype_은_항상_fp32():
    assert rt.PIIDetector._resolve_dtype("float16", "cuda") is torch.float32
    assert rt.PIIDetector._resolve_dtype("float32", "cpu") is torch.float32
    with pytest.raises(ValueError, match="Unsupported PII dtype"):
        rt.PIIDetector._resolve_dtype("bfloat16", "cuda")


def test_device_는_cpu_cuda_만():
    assert rt.PIIDetector._resolve_device("cpu") == "cpu"
    assert rt.PIIDetector._resolve_device("auto") in {"cpu", "cuda"}
    with pytest.raises(ValueError, match="Unsupported PII device"):
        rt.PIIDetector._resolve_device("mps")
