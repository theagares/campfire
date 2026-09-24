"""
app/adapters/proxy/protobuf.py
스키마 없이 protobuf 를 풀고 다시 쌌다.

Claude 는 메시지를 보낼 때 사용자가 친 텍스트(와 인라인한 텍스트 파일)를
claudeai-rpc 의 protobuf 본문에 담는다. 파일 업로드가 아니라 이 경로라서 지금은
검사 밖으로 샌다. 막으려면 그 텍스트 필드를 찾아 마스킹본으로 바꿔 다시 싸야 한다.

.proto 스키마가 없다. 그래서 wire 포맷만으로 다룬다 — wire 포맷은 필드 번호와
타입(varint/64bit/length-delimited/32bit)만 담고 "이게 문자열이냐 중첩 메시지냐"는
구분하지 않는다. length-delimited(타입 2)는 문자열일 수도, 바이트일 수도, 또 다른
메시지일 수도 있다. 그래서 재귀로 "메시지로 읽히면 메시지, 아니면 원시 바이트"로
추정해 트리를 만들고, 손댄 곳만 바꿔 다시 직렬화한다.

**보존이 핵심이다.** 우리가 안 건드린 필드는 들어온 바이트 그대로 나가야 한다 —
같은 값도 varint 인코딩이 여러 가지라(예: 비정규 varint) 재직렬화가 원본과
바이트 단위로 같다는 보장이 없다. 그래서 파싱할 때 각 필드의 **원본 바이트 구간**을
들고 있다가, 안 바뀐 필드는 그 구간을 그대로 되쓴다. 바뀐 필드만 새로 인코딩한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field


class DecodeError(Exception):
    """protobuf 로 읽히지 않는다. 호출부는 원문을 건드리지 않고 그대로 둔다."""


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    start = i
    while True:
        if i >= len(buf):
            raise DecodeError("varint 가 잘렸다")
        b = buf[i]
        result |= (b & 0x7F) << shift
        i += 1
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            raise DecodeError("varint 가 너무 길다")
    # (unreachable)
    del start


def _encode_varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("음수 varint")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


@dataclass
class Field:
    """protobuf 필드 하나. raw 는 태그부터 값 끝까지의 원본 바이트 구간."""

    number: int
    wire_type: int
    raw: bytes                       # 안 바뀌면 이걸 그대로 되쓴다
    value: bytes | None = None       # length-delimited 의 원시 값(타입 2만)
    message: "Message | None" = None  # 값이 메시지로 읽히면 그 트리
    _dirty: bool = False

    def set_bytes(self, new_value: bytes) -> None:
        """이 필드의 length-delimited 값을 바꾼다(문자열 필드 마스킹용)."""
        if self.wire_type != 2:
            raise ValueError("length-delimited 필드가 아니다")
        self.value = new_value
        self.message = None
        self._dirty = True

    def serialize(self) -> bytes:
        if not self._dirty and self.message is None:
            return self.raw
        if self.message is not None:
            body = self.message.serialize()
        else:
            body = self.value or b""
        tag = (self.number << 3) | self.wire_type
        return _encode_varint(tag) + _encode_varint(len(body)) + body


@dataclass
class Message:
    fields: list[Field] = dc_field(default_factory=list)

    def serialize(self) -> bytes:
        return b"".join(f.serialize() for f in self.fields)

    def walk(self):
        """(경로, Field) 를 재귀로 낸다. 경로는 필드 번호의 튜플."""
        for f in self.fields:
            yield (f.number,), f
            if f.message is not None:
                for sub_path, sub in f.message.walk():
                    yield (f.number, *sub_path), sub


# length-delimited 를 메시지로 볼지 결정하는 최소 판별.
# 메시지로 못 읽히면 원시 바이트(문자열/바이트)로 둔다.
def _try_parse_message(buf: bytes) -> "Message | None":
    if not buf:
        return None
    try:
        msg = _parse(buf, depth=0)
    except DecodeError:
        return None
    # 전부 소비하지 못했으면 메시지가 아니다.
    return msg


def _parse(buf: bytes, depth: int) -> Message:
    if depth > 100:
        raise DecodeError("너무 깊다")
    msg = Message()
    i = 0
    n = len(buf)
    while i < n:
        tag_start = i
        tag, i = _read_varint(buf, i)
        wire_type = tag & 0x7
        number = tag >> 3
        if number == 0:
            raise DecodeError("필드 번호 0")
        if wire_type == 0:  # varint
            _, i = _read_varint(buf, i)
        elif wire_type == 1:  # 64bit
            i += 8
        elif wire_type == 2:  # length-delimited
            length, i = _read_varint(buf, i)
            if i + length > n:
                raise DecodeError("length 가 버퍼를 넘는다")
            val = buf[i:i + length]
            i += length
            raw = buf[tag_start:i]
            sub = _try_parse_message(val)
            msg.fields.append(Field(number, wire_type, raw, value=val, message=sub))
            continue
        elif wire_type == 5:  # 32bit
            i += 4
        else:
            raise DecodeError(f"모르는 wire type {wire_type}")
        if i > n:
            raise DecodeError("값이 버퍼를 넘는다")
        msg.fields.append(Field(number, wire_type, buf[tag_start:i]))
    return msg


def decode(buf: bytes) -> Message:
    """최상위 protobuf 메시지로 푼다. 못 읽으면 DecodeError."""
    return _parse(buf, depth=0)


def looks_like_text(data: bytes, *, min_len: int = 1) -> bool:
    """이 length-delimited 값이 사람이 읽는 텍스트인가.

    문자열 필드만 검사·마스킹 대상으로 삼기 위한 판별이다. 메시지로 읽히면
    문자열이 아니고, UTF-8 로 안 풀리거나 제어문자가 많으면 바이트다.
    """
    if len(data) < min_len:
        return False
    try:
        s = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not s.strip():
        return False
    odd = sum(1 for ch in s if ord(ch) < 32 and ch not in "\t\n\r")
    return odd * 20 <= len(s)  # 제어문자 5% 미만
