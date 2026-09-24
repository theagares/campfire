"""스키마 없는 protobuf 풀기/다시 싸기.

지키려는 것 하나: **안 건드린 필드는 바이트 단위로 그대로 나간다.** 안 그러면
Claude 가 우리가 재직렬화한 요청을 거부하거나, 조용히 다른 뜻으로 읽는다.
"""

from __future__ import annotations

import struct

from app.adapters.proxy import protobuf as pb


def _tag(num, wt):
    return pb._encode_varint((num << 3) | wt)


def _ld(num, data):
    return _tag(num, 2) + pb._encode_varint(len(data)) + data


def _vi(num, n):
    return _tag(num, 0) + pb._encode_varint(n)


def test_왕복이_바이트까지_같다():
    raw = (
        _vi(1, 150)
        + _ld(2, "안녕하세요 테스트".encode())
        + _ld(3, _vi(1, 7) + _ld(2, b"nested"))  # 중첩 메시지
        + _tag(4, 1) + struct.pack("<d", 3.14)     # 64bit
        + _tag(5, 5) + struct.pack("<i", 42)       # 32bit
    )
    msg = pb.decode(raw)
    assert msg.serialize() == raw, "손 안 댔는데 재직렬화가 원본과 다르다"


def test_문자열_필드만_바꾸고_나머지는_보존():
    raw = _vi(1, 999) + _ld(2, "원본 텍스트".encode()) + _ld(9, b"\x00\x01\x02binary")
    msg = pb.decode(raw)
    # 2번 필드(문자열)만 바꾼다
    target = next(f for f in msg.fields if f.number == 2)
    target.set_bytes("마스킹된 텍스트".encode())
    out = pb.decode(msg.serialize())
    assert next(f for f in out.fields if f.number == 2).value.decode() == "마스킹된 텍스트"
    # 나머지는 그대로
    assert next(f for f in out.fields if f.number == 1).raw == _vi(1, 999)
    assert next(f for f in out.fields if f.number == 9).value == b"\x00\x01\x02binary"


def test_중첩_메시지_안의_문자열도_찾는다():
    inner = _ld(3, "깊은 곳의 주민번호".encode())
    raw = _ld(1, inner)
    msg = pb.decode(raw)
    paths = {path: f for path, f in msg.walk()}
    assert (1, 3) in paths, "중첩 필드를 못 찾았다"
    assert paths[(1, 3)].value.decode() == "깊은 곳의 주민번호"


def test_중첩_안의_필드를_바꿔도_왕복된다():
    inner = _ld(3, "before".encode())
    raw = _ld(1, inner) + _vi(2, 5)
    msg = pb.decode(raw)
    for path, f in msg.walk():
        if path == (1, 3):
            f.set_bytes("after-longer".encode())  # 길이가 달라진다 → 상위 length 도 갱신돼야
    out = pb.decode(msg.serialize())
    got = {path: f for path, f in out.walk()}
    assert got[(1, 3)].value.decode() == "after-longer"
    assert got[(2,)].raw == _vi(2, 5)


def test_문자열_판별():
    assert pb.looks_like_text("주민번호 900101-1234567".encode())
    assert not pb.looks_like_text(b"\x00\x01\x02\x03")
    assert not pb.looks_like_text(b"")
    assert not pb.looks_like_text(b"   ")  # 공백만


def test_안_읽히면_예외():
    import pytest

    with pytest.raises(pb.DecodeError):
        pb.decode(b"\xff\xff\xff\xff")  # 잘린 varint


def test_바이너리는_메시지로_오인하지_않는다():
    # 임의 바이트가 우연히 메시지로 읽히면 안 바뀐 필드도 재직렬화 대상이 된다.
    raw = _ld(1, b"\x00\x01\x02\x03\x04plain")
    msg = pb.decode(raw)
    f = msg.fields[0]
    # 메시지로 못 읽혔으면 message is None, 원시 값 보존
    assert f.message is None
    assert msg.serialize() == raw


def _nested(depth: int, leaf: bytes) -> bytes:
    data = leaf
    for _ in range(depth):
        data = _ld(1, data)
    return data


def test_아주_깊은_중첩도_안_터지고_보존된다():
    # 예전엔 _try_parse_message 가 depth 를 0 으로 리셋해 깊이 가드가 죽어 있었고,
    # 깊게 중첩된 본문은 RecursionError 로 떨어졌다. 이제 100 을 넘으면 그 아래는
    # 메시지로 더 안 풀고 원시 바이트로 둔다 — 안 건드렸으니 바이트까지 왕복한다.
    deep = _nested(2000, _ld(3, "안녕".encode()))
    msg = pb.decode(deep)            # 예외(RecursionError) 없이 풀린다
    assert msg.serialize() == deep   # 손 안 댔으니 원본 그대로
