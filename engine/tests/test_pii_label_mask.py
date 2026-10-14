"""PII CRF label mask의 CPU 일괄 생성 회귀 테스트."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import torch

_RUNTIME_DIR = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "models"
    / "pii_engine"
    / "runtime"
)


def _load_runtime():
    sys.path.insert(0, str(_RUNTIME_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "_pii_runtime_label_mask", _RUNTIME_DIR / "local_pii_inference.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_RUNTIME_DIR))


build_label_mask = _load_runtime().build_label_mask


def test_label_mask_selects_only_first_valid_token_for_each_word():
    # word 1이 비연속으로 다시 나타나도 기존 seen-set 규칙처럼 선택하지 않는다.
    word_ids = [[None, 0, 0, 1, 1, None, 1, 2]]
    offsets = [[(0, 0), (0, 1), (1, 2), (3, 4), (4, 5), (0, 0), (6, 7), (8, 9)]]

    actual = build_label_mask(word_ids, offsets)

    assert actual.dtype is torch.bool
    assert actual.device.type == "cpu"
    assert actual.tolist() == [[False, True, False, True, False, False, False, True]]


def test_zero_end_does_not_consume_word_id():
    # 첫 등장 위치가 특수/빈 offset이면 뒤의 첫 유효 위치를 선택해야 한다.
    word_ids = [[3, 3, None]]
    offsets = [[(0, 0), (0, 2), (0, 0)]]

    assert build_label_mask(word_ids, offsets).tolist() == [[False, True, False]]
