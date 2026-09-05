"""긴 PII 항목이 외부 API(Solar)로 나갈 때 가려지지 않고 새던 문제를 막는다.

배경: 인젝션 2차 위치특정은 청크 텍스트를 Upstage Solar 로 보낸다. 보내기 전에
PII 를 자리표시자로 바꾸는데(masker/redact_map), 그 재료가 되는 청크 기준 좌표를
_pii_spans_for_chunk 가 만든다.

예전엔 청크 경계를 걸친 항목을 통째로 **제외**했다. 근거는 "청크가 100자씩 겹치니
걸친 항목도 이웃 청크에서는 온전히 들어온다" 였는데, 그 보장은 항목이 겹침보다
짧을 때만 성립한다. chunk_size=1000 / step=900 에서 어느 청크에도 온전히 담기지
못하는 최소 길이는 102자다.

그보다 긴 항목은 모든 청크에서 빠지고, pii_spans 가 비면 build_redaction 이
"가릴 게 없다" 고 보아 그 자리를 **원문 그대로** 외부로 보낸다. 원문을 외부 AI 에
넘기지 않으려고 만든 제품에서 가장 나쁜 실패다.
"""

from __future__ import annotations

import pytest

from app import config
from app.core.masker.redact_map import build_redaction
from app.core.pipeline.orchestrator import _pii_spans_for_chunk, _split_chunks


def _covered(item: dict, text: str) -> set[int]:
    """모든 청크에 걸쳐 실제로 가려지는 원문 위치의 집합."""
    out: set[int] = set()
    for ch in _split_chunks(text, config.CHUNK_SIZE):
        for sp in _pii_spans_for_chunk([item], ch):
            out.update(range(ch["offset"] + sp["start"], ch["offset"] + sp["end"]))
    return out


def test_guarantee_threshold_is_what_the_math_says():
    """겹침이 보장하는 최대 길이를 못 박아 둔다.

    CHUNK_SIZE 나 겹침을 나중에 바꾸면 이 값이 달라진다. 그때 이 테스트가 먼저
    깨져야 _pii_spans_for_chunk 주석의 근거도 같이 고쳐진다."""
    chunk, overlap = config.CHUNK_SIZE, 100
    step = chunk - overlap
    assert step > 0
    # 항목은 청크 시작에서 최대 step-1 만큼 뒤에 놓일 수 있다.
    assert chunk - (step - 1) == 101, "온전히 담김이 보장되는 최대 길이"


@pytest.mark.parametrize("start, length", [(850, 200), (880, 150), (1750, 300)])
def test_long_span_is_fully_covered_not_dropped(start, length):
    """어느 청크에도 온전히 담기지 않는 항목도 빠짐없이 가려져야 한다.

    예전 동작(경계를 걸치면 제외)에서는 covered 가 통째로 비었다 — 그 구간이
    원문 그대로 외부로 나갔다는 뜻이다."""
    text = "가" * 4000
    item = {"start": start, "end": start + length, "type": "ADDRESS", "confidence": 0.9}
    assert _covered(item, text) == set(range(start, start + length))


def test_short_span_still_works():
    """경계 처리를 바꾸느라 평범한 짧은 항목이 깨지면 안 된다."""
    text = "가" * 4000
    item = {"start": 120, "end": 134, "type": "ID_NUMBER", "confidence": 0.99}
    assert _covered(item, text) == set(range(120, 134))


def test_span_outside_chunk_is_not_included():
    """겹치지 않는 항목까지 끌어오면 엉뚱한 자리를 가린다."""
    ch = {"text": "가" * 1000, "offset": 1000}
    far = {"start": 10, "end": 30, "type": "PHONE", "confidence": 0.9}
    assert _pii_spans_for_chunk([far], ch) == []


def test_clamped_span_is_actually_redacted_before_outbound():
    """잘린 좌표가 실제로 외부 전송본에서 가려지는지 끝까지 확인한다.

    좌표만 맞고 redact 가 안 되면 의미가 없다 — build_redaction 은 항목에 text
    필드가 없으면 좌표를 신뢰하므로(masker.validate_and_fix), 잘린 좌표도 그대로
    자리표시자로 바뀌어야 한다."""
    secret = "서울특별시강남구테헤란로" * 20          # 240자 — 어느 청크에도 안 들어간다
    text = "가" * 850 + secret + "나" * 850
    item = {"start": 850, "end": 850 + len(secret), "type": "ADDRESS", "confidence": 0.9}

    leaked = []
    for ch in _split_chunks(text, config.CHUNK_SIZE):
        spans = _pii_spans_for_chunk([item], ch)
        outbound = build_redaction(ch["text"], spans).text
        # 이 청크가 들고 있던 원문 조각이 전송본에 그대로 남아 있으면 유출이다.
        piece_lo = max(850, ch["offset"])
        piece_hi = min(850 + len(secret), ch["offset"] + len(ch["text"]))
        if piece_hi - piece_lo >= 20:      # 우연한 짧은 일치는 세지 않는다
            piece = text[piece_lo:piece_hi]
            if piece in outbound:
                leaked.append((ch["offset"], len(piece)))

    assert not leaked, f"외부 전송본에 원문 조각이 남았다: {leaked}"
