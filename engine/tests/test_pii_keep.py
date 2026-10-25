"""문맥 판단 PII 탐지기(taehwan direct_full) — 탐지기 교체·keep 표시 규칙·덩어리 나누기와 keep_scorer 순수 함수(모델 없이)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from app import config
from app.core import model_status
from app.core.detectors import registry
from app.core.detectors.pii import encoder, keep

_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "app" / "models" / "pii_engine" / "runtime"


def _load_scorer():
    sys.path.insert(0, str(_RUNTIME_DIR))  # keep_scorer 가 같은 폴더의 local_pii_inference(후처리)를 가져온다
    try:
        spec = importlib.util.spec_from_file_location("_pii_keep_scorer", _RUNTIME_DIR / "keep_scorer.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_RUNTIME_DIR))


ks = _load_scorer()


def _detector(monkeypatch, reply_for):
    """하위 프로세스 없이 — _rpc 가 덩어리 글자를 받아 가짜 답을 준다."""
    det = keep.KeepPiiDetector.__new__(keep.KeepPiiDetector)
    det.residency = type("R", (), {"touch": lambda self: None})()
    seen = []

    async def ensure():
        return None

    async def rpc(request):
        seen.extend(request["texts"])
        return {"entities": [reply_for(t) for t in request["texts"]], "threshold": 0.6}

    monkeypatch.setattr(det, "_ensure_process", ensure, raising=False)
    monkeypatch.setattr(det, "_rpc", rpc, raising=False)
    return det, seen


def test_keep_only_for_company_contact_labels_under_threshold(monkeypatch):
    text = "문의 031-676-4069 담당 김민수 010-2345-6789 help@fursys.com"

    def reply(t):
        at = lambda s: (t.index(s), t.index(s) + len(s))  # noqa: E731
        return [
            {"begin": at("031-676-4069")[0], "end": at("031-676-4069")[1], "label": "QT_PHONE", "maskProb": 0.01},
            {"begin": at("김민수")[0], "end": at("김민수")[1], "label": "PS_NAME", "maskProb": 0.01},  # 이름은 낮아도 가림
            {"begin": at("010-2345-6789")[0], "end": at("010-2345-6789")[1], "label": "QT_MOBILE", "maskProb": 0.99},
            {"begin": at("help@fursys.com")[0], "end": at("help@fursys.com")[1], "label": "TMI_EMAIL", "maskProb": None},
        ]

    det, _ = _detector(monkeypatch, reply)
    items = asyncio.run(det.detect(text))
    assert [(i["text"], i["type"], i.get("keep", False)) for i in items] == [
        ("031-676-4069", "PHONE", True), ("김민수", "PERSON_NAME", False),
        ("010-2345-6789", "PHONE", False), ("help@fursys.com", "EMAIL", False)]  # 점수 없음 → 가림
    assert items[1]["maskProb"] == 0.01 and "maskProb" not in items[3]
    assert all(i["source"] == "encoder" for i in items)


def test_long_text_is_sent_in_chunks_with_offsets(monkeypatch):
    monkeypatch.setattr(config, "PII_KEEP_CHUNK_CHARS", 12)
    text = "가나다라마바\n김민수 입니다\n\n끝줄은 아주 길어서 한 줄이 덩어리보다 길다\n"
    det, seen = _detector(monkeypatch, lambda t: [{"begin": 0, "end": 2, "label": "PS_NAME", "maskProb": 0.9}])
    items = asyncio.run(det.detect(text))
    assert "".join(seen) == text and all(len(c) <= 12 for c in seen)
    assert [i["text"] for i in items] == [c[:2] for c in seen]  # 덩어리 시작 오프셋이 원문 좌표로 돌아온다


def test_split_chunks_cut_order_newline_sentence_space_hard():
    def parts(text, size):
        chunks = keep.split_chunks(text, size)
        assert "".join(c for c, _ in chunks) == text
        assert all(text[o:o + len(c)] == c for c, o in chunks)
        assert all(len(c) <= size for c, _ in chunks)
        return [c for c, _ in chunks]

    assert parts("a\nbb\nccc\n" * 50, 20)[0] == "a\nbb\nccc\n" * 2 + "a\n"           # 줄바꿈
    assert parts("첫 문장이다. 둘째 문장 서울특별시 강남구", 20)[0] == "첫 문장이다. "      # 줄바꿈 없으면 문장 끝
    assert parts("주소는 서울특별시 강남구 테헤란로", 12)[0] == "주소는 서울특별시 "        # 문장 끝도 없으면 공백
    assert parts("가" * 30, 12) == ["가" * 12, "가" * 12, "가" * 6]                    # 공백도 없으면 size
    assert keep.split_chunks("  \n \n", 20) == []


def test_registry_switches_only_when_enabled_and_ready(monkeypatch):
    built = []
    monkeypatch.setattr(encoder, "build", lambda: built.append("v2") or object())
    monkeypatch.setattr(keep, "build", lambda: built.append("keep") or keep.KeepPiiDetector.__new__(keep.KeepPiiDetector))
    monkeypatch.setattr(registry, "_pii_detector", None)
    monkeypatch.setattr(config, "PII_KEEP_ENABLED", True)
    monkeypatch.setattr(model_status, "pii_keep_ready", lambda: False)
    registry.get_pii_detector()  # 켰지만 아직 받는 중 → v2
    monkeypatch.setattr(model_status, "pii_keep_ready", lambda: True)
    first = registry.get_pii_detector()  # 다 받은 뒤 첫 호출에서 바꾼다
    assert registry.get_pii_detector() is first
    assert built == ["v2", "keep"]
    monkeypatch.setattr(registry, "_pii_detector", None)
    monkeypatch.setattr(config, "PII_KEEP_ENABLED", False)
    registry.get_pii_detector()
    assert built == ["v2", "keep", "v2"]


def test_split_sentences_and_bio_decode():
    text = "문의: 031-676-4069.\n담당 김민수! 끝"
    assert [text[a:b] for a, b in ks.split_sentences(text)] == ["문의: 031-676-4069.", "담당 김민수!", "끝"]
    idx = ks.BIO.index
    offsets = [(0, 0), (0, 2), (2, 5), (5, 6), (0, 0)]
    tags = [0, idx("B-NAME"), idx("I-NAME"), idx("I-ADDRESS"), 0]
    # I-ADDRESS 가 다른 종류 뒤에 오면 새 개체로 시작한다(원본 decode_bio 와 같다)
    assert ks._decode_bio(offsets, tags) == [{"begin": 0, "end": 5, "label": "NAME"}, {"begin": 5, "end": 6, "label": "ADDRESS"}]


def test_viterbi_respects_bio_constraints():
    import torch
    start, trans = ks._crf_constraints()
    em = torch.zeros(2, 3, ks.BIO_SIZE)
    em[:, :, ks.BIO.index("I-EMAIL")] = 5.0  # I- 로 시작하는 경로는 막혀 있다
    em[1, 2] = 0.0
    em[1, 2, ks.BIO.index("B-NAME")] = 9.0   # mask 밖 — 무시돼야 한다
    mask = torch.tensor([[True, True, True], [True, True, False]])
    paths = ks._viterbi(em, mask, start, torch.zeros(ks.BIO_SIZE), trans).tolist()
    assert ks.BIO[paths[0][0]] != "I-EMAIL" and ks.BIO[paths[1][0]] != "I-EMAIL"
    assert paths[1][2] == 0  # mask 밖은 0
    # 묶음으로 돌려도 한 줄씩 돌린 것과 같다
    for j, n in ((0, 3), (1, 2)):
        one = ks._viterbi(em[j:j + 1, :n], mask[j:j + 1, :n], start, torch.zeros(ks.BIO_SIZE), trans).tolist()[0]
        assert one == paths[j][:n]


def test_load_weights_reads_tensors_into_modules_strictly(tmp_path):
    import torch
    from safetensors.torch import save_file
    src = torch.nn.Linear(3, 2)
    path = tmp_path / "w.safetensors"
    crf = torch.arange(4, dtype=torch.float32)
    save_file({"m.weight": src.weight.detach().contiguous(), "m.bias": src.bias.detach(),
               "stage_a.crf.transitions": crf}, str(path))
    dst = torch.nn.Linear(3, 2)
    out = ks._load_weights(path, ((dst, "m."),))
    assert torch.equal(dst.weight, src.weight) and torch.equal(dst.bias, src.bias)
    assert torch.equal(out["transitions"], crf)
    with pytest.raises(RuntimeError, match="키 불일치"):
        ks._load_weights(path, ((torch.nn.Linear(3, 2, bias=False), "m."),))  # 파일에 남는 키(m.bias)
    with pytest.raises(RuntimeError, match="모양 불일치"):
        ks._load_weights(path, ((torch.nn.Linear(4, 2), "m."),))


def test_postprocess_regex_only_adds_or_widens_never_shrinks():
    s = "회사 대표번호 031-676-4069, 주민번호 900101-1234567, 휴대폰 010-2345-6789, 계좌 446-1283-1190-29"
    phone, mobile, acct = s.index("031"), s.index("010-2345"), s.index("446")
    ents = [{"begin": phone, "end": phone + 11, "label": "QT_PHONE", "maskProb": 0.2},  # 끝 숫자 하나 빠진 구간, KEEP 점수
            {"begin": mobile, "end": mobile + 13, "label": "QT_PHONE", "maskProb": 0.1},  # 휴대폰을 일반 전화로 잘못
            {"begin": acct, "end": acct + 16, "label": "QT_ACCOUNT_NUMBER", "maskProb": 0.9}]  # 정규식은 끝 '-29' 를 못 잡는다
    out = {s[e["begin"]:e["end"]]: (e["label"], e["maskProb"]) for e in ks.postprocess(s, ents)}
    assert out == {
        "031-676-4069": ("QT_PHONE", 0.2),  # 같은 라벨 — 구간은 합집합으로 넓히고 점수는 모델 (KEEP 이 산다)
        "900101-1234567": ("QT_RESIDENT_NUMBER", None),  # 모델이 놓친 것 — 점수 없이 더함 → 항상 가림
        "010-2345-6789": ("QT_PHONE", None),  # 라벨이 다름 — 점수를 지워 항상 가림
        "446-1283-1190-29": ("QT_ACCOUNT_NUMBER", 0.9),  # 정규식 구간이 더 짧아도 모델 구간을 줄이지 않는다
    }

    s2 = "주소: 서울특별시 마포구 성미산로 12길 34, 101동 1203호"
    a, c = s2.index("서울"), s2.index("101동")
    ents2 = [{"begin": a, "end": s2.index("34") + 2, "label": "LC_ADDRESS", "maskProb": 0.3},
             {"begin": c, "end": len(s2), "label": "LC_ADDRESS", "maskProb": 0.7}]
    assert [(s2[e["begin"]:e["end"]], e["maskProb"]) for e in ks.postprocess(s2, ents2)] == [(s2[a:], 0.7)]  # 합치면 최댓값


def test_type_maps_round_trip():
    assert set(ks.TYPE_TO_APP) == set(ks.TYPES) and set(ks.TYPE_TO_APP.values()) <= set(encoder.LABEL_MAP)
    assert config.PII_KEEP_LABELS <= set(encoder.LABEL_MAP)
