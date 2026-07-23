"""PII 룰베이스 detector 단위 테스트 (PLAN §5)."""

import asyncio

from app.core.detectors.pii import rule_based
from app.core.detectors.rule_engine import _luhn, _rrn_checksum


def _detect(text):
    det = rule_based.build()
    return asyncio.run(det.detect(text, meta={}))


def _types(dets):
    return {d["type"] for d in dets}


def test_rrn_checksum_validator():
    assert _rrn_checksum("900101-1234568") is True   # 체크섬 유효
    assert _rrn_checksum("900101-1234567") is False  # 체크섬 불일치


def test_luhn_validator():
    assert _luhn("4111 1111 1111 1111") is True
    assert _luhn("4111 1111 1111 1112") is False


def test_detects_valid_rrn():
    dets = _detect("제 주민등록번호는 900101-1234568 입니다.")
    assert "ID_NUMBER" in _types(dets)
    hit = next(d for d in dets if d["type"] == "ID_NUMBER")
    assert hit["text"] == "900101-1234568"


def test_rejects_invalid_rrn_checksum():
    # 체크섬이 틀린 번호는 ID_NUMBER 로 잡히지 않아야 한다
    dets = _detect("잘못된번호 900101-1234561 참고")
    assert "ID_NUMBER" not in _types(dets)


def test_detects_email_and_phone():
    dets = _detect("연락처: hong@example.com / 010-1234-5678")
    types = _types(dets)
    assert "EMAIL" in types
    assert "PHONE" in types


def test_detects_credit_card_luhn():
    dets = _detect("카드: 4111-1111-1111-1111")
    assert "CREDIT_CARD" in _types(dets)


def test_bank_account_keyword_anchored():
    dets = _detect("입금 계좌번호: 110-234-567890 신한은행")
    assert "BANK_ACCOUNT" in _types(dets)


def test_person_name_keyword_anchored():
    dets = _detect("성명: 홍길동\n부서: 개발팀")
    names = [d for d in dets if d["type"] == "PERSON_NAME"]
    assert any(d["text"] == "홍길동" for d in names)


def test_no_false_positive_on_plain_text():
    dets = _detect("오늘 날씨가 참 좋네요. 산책하기 딱 좋은 날입니다.")
    assert dets == []
