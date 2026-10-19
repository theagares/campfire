"""PII 주소 보강 — 모델이 쉼표·괄호에서 끊거나 놓친 주소를 동·호까지 하나로 (실측 사례 그대로)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "app" / "models" / "pii_engine" / "runtime"


def _load_runtime():
    sys.path.insert(0, str(_RUNTIME_DIR))
    try:
        spec = importlib.util.spec_from_file_location("_pii_runtime_address", _RUNTIME_DIR / "local_pii_inference.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_RUNTIME_DIR))


rt = _load_runtime()
PRE = "주소는 "


def _addr(text: str, part: str) -> dict:
    i = text.index(part)
    return {"form": part, "label": "LC_ADDRESS", "begin": i, "end": i + len(part)}


def _post(text: str, entities: list[dict]) -> list[str]:
    out = rt.address_postprocess(text, rt.merge_lc_address(entities, text))
    return [text[e["begin"]:e["end"]] for e in out if e["label"] == "LC_ADDRESS"]


def test_쉼표_뒤_동호를_주소에_붙인다():
    text = PRE + "서울특별시 마포구 성미산로 12길 34, 101동 1203호다."
    assert _post(text, [_addr(text, "서울특별시 마포구 성미산로 12길 34")]) == ["서울특별시 마포구 성미산로 12길 34, 101동 1203호"]


def test_모델이_놓친_괄호_주소를_정규식으로_찾는다():
    text = PRE + "서울특별시 마포구 성미산로 12길 34(101동 1203호)다."
    assert _post(text, []) == ["서울특별시 마포구 성미산로 12길 34(101동 1203호)"]


def test_시도를_줄여_써도_찾는다():
    text = PRE + "서울시 마포구 성미산로 12길 34, 101동 1203호다."
    assert _post(text, []) == ["서울시 마포구 성미산로 12길 34, 101동 1203호"]


def test_쪼개진_주소는_하나로_합친다():
    text = PRE + "서울특별시 마포구 성미산로12길 34, 101동 1203호다."
    got = _post(text, [_addr(text, "서울특별시"), _addr(text, "성미산로12길 34")])
    assert got == ["서울특별시 마포구 성미산로12길 34, 101동 1203호"]


def test_시도가_없어도_모델이_잡은_주소엔_동호를_붙인다():
    text = PRE + "마포구 성미산로 12길 34, 101동 1203호다."
    assert _post(text, [_addr(text, "마포구 성미산로 12길 34")]) == ["마포구 성미산로 12길 34, 101동 1203호"]


def test_지번_주소와_모델이_더_넓게_잡은_건물명은_지킨다():
    text = PRE + "대전광역시 동구 가양동 113-20 한빛아파트 111동 1389호로 보내 주세요."
    wide = "대전광역시 동구 가양동 113-20 한빛아파트 111동 1389호"
    assert _post(text, [_addr(text, wide)]) == [wide]
    assert _post(text, []) == [wide]  # 모델이 놓쳐도 정규식이 건물명·동·호까지


def test_주소가_아닌_문장은_그대로():
    text = "정산허브는 2027년 7월에 출시하고 목표 매출은 45억 원이다. 블루웨이브 개발 인원은 60명이다."
    assert _post(text, []) == []
    text2 = "주소는서울특별시 마포구 성미산로 12길 34다."  # 앞 단어까지 주소로 먹지 않는다
    assert _post(text2, []) == ["서울특별시 마포구 성미산로 12길 34"]
