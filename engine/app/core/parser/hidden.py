"""
app/core/parser/hidden.py
사람 눈에 안 보이게 숨긴 글 — 인젝션 모델 판정과 무관하게 규칙으로 가리고 잠근다(사용자 결정 2026-10-29).

파서는 숨긴 구간을 표식(사용 영역 문자 HS + 이유 코드 한 글자 + 글 + HE)으로 감싸 본문에 남긴다 — 그림 표시(images.py)와
같은 방식. orchestrator 가 extract() 로 표식을 지우고 구간을 HIDDEN_TEXT 항목으로 바꾼다. 보이지 않는 유니코드(폭 없는
문자 4글자 이상·태그 문자·방향 제어)는 형식과 관계없이 모든 글(프롬프트 포함)에서 invisible_spans() 로 찾는다.
오탐을 줄이려고 사람이 정말 못 보는 경우만 잡는다 — 글자색은 실제 배경과 비교한다(어두운 칸의 흰 글씨는 보인다).
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from typing import Callable

TYPE = "HIDDEN_TEXT"
HS, HE = "", ""
REASONS = {
    "w": "흰 바탕 흰 글씨",
    "c": "배경과 같은 색 글씨",
    "h": "숨김 서식",
    "s": "1pt 이하 글자",
    "o": "화면 밖 글자",
    "u": "보이지 않는 문자",
}
_MARKS = re.compile(f"[{HS}{HE}]")
# 폭 없는 문자 U+200B–200F·2060–2064·FEFF 는 4글자 이상 이어질 때만 — 3글자 이하는 숨길 내용이 없고 웹 복사·편집기 흔적
# (단어 사이 U+200B, Copilot 끝 U+200B U+200C, 이모지 잇기 U+200D)이 흔하다(마스터 결정 2026-10-30). 잡지도 지우지도 않는다.
_ZERO_WIDTH = re.compile("[​-‏⁠-⁤﻿]{4,}")
# 방향 제어 U+202A–202E·2066–2069(글 순서 뒤집기)와 태그 문자 U+E0000–E007F(ASCII smuggling)는 한 글자도
_ALWAYS = re.compile("[‪-‮⁦-⁩\U000e0000-\U000e007f]+")
_TAGS_END = "\U000e007f"
_BLACK_FLAG = "\U0001f3f4"

# MCP 는 사람이 검토하는 패널 없이 결과를 곧바로 AI 에게 돌려준다. 그 경로에서는
# Unicode Default_Ignorable_Code_Point 전부와 화면에 나타나지 않는 제어 문자를 한
# 글자부터 잡는다. Cf 범주만 보면 CGJ·한글 채움·변형 선택자처럼 다른 범주인 문자가
# 빠지므로 Unicode 파생 속성의 나머지 범위를 함께 둔다.
_DEFAULT_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCAF),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_DEFAULT_IGNORABLE = frozenset(
    cp
    for start, end in _DEFAULT_IGNORABLE_RANGES
    for cp in range(start, end + 1)
)


def _is_emoji_base(ch: str) -> bool:
    """VS16·ZWJ 를 정상 이모지 안에서만 살리기 위한 보수적인 이모지 바탕 판정."""
    cp = ord(ch)
    return (
        ch in "#*0123456789©®™〰〽㊗㊙"
        or 0x2190 <= cp <= 0x21FF
        or 0x2300 <= cp <= 0x23FF
        or 0x2500 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or 0x1F000 <= cp <= 0x1FAFF
    )


def _is_emoji_component(ch: str) -> bool:
    cp = ord(ch)
    return _is_emoji_base(ch) or ch == "️" or 0x1F3FB <= cp <= 0x1F3FF


def _flag_tag_exceptions(text: str) -> set[int]:
    """검은 깃발 + 태그 문자 + CANCEL TAG 로 된 지역 깃발의 태그 위치."""
    safe: set[int] = set()
    i = 0
    while i < len(text):
        if text[i] != _BLACK_FLAG:
            i += 1
            continue
        j = i + 1
        while j < len(text) and 0xE0020 <= ord(text[j]) <= 0xE007E:
            j += 1
        if j > i + 1 and j < len(text) and text[j] == _TAGS_END:
            safe.update(range(i + 1, j + 1))
            i = j + 1
        else:
            i += 1
    return safe


def _is_all_invisible(ch: str) -> bool:
    cp = ord(ch)
    category = unicodedata.category(ch)
    return (
        category == "Cf"
        or (category == "Cc" and not ch.isspace())
        or cp in _DEFAULT_IGNORABLE
    )


def _all_invisible_spans(text: str) -> list[dict]:
    """MCP 전용: 보이지 않는 문자를 길이 제한 없이 찾되 정상 이모지는 보존한다."""
    flag_tags = _flag_tag_exceptions(text) if _BLACK_FLAG in text else set()
    out: list[dict] = []
    start: int | None = None

    for i, ch in enumerate(text):
        is_hidden = _is_all_invisible(ch)
        if i in flag_tags:
            is_hidden = False
        elif ch == "‍":  # 이모지 사이의 ZERO WIDTH JOINER
            is_hidden = not (
                i > 0 and i + 1 < len(text)
                and _is_emoji_component(text[i - 1])
                and _is_emoji_base(text[i + 1])
            )
        elif ch == "️":  # 이모지 바로 뒤의 VARIATION SELECTOR-16
            is_hidden = not (i > 0 and _is_emoji_base(text[i - 1]))

        if is_hidden:
            if start is None:
                start = i
        elif start is not None:
            out.append({"start": start, "end": i, "reason": REASONS["u"]})
            start = None

    if start is not None:
        out.append({"start": start, "end": len(text), "reason": REASONS["u"]})
    return out


def wrap(text: str, code: str) -> str:
    """숨긴 글을 표식으로 감싼다. code 는 REASONS 의 키."""
    return f"{HS}{code}{text}{HE}" if text else ""


def extract(text: str) -> tuple[str, list[dict], Callable[[int], int]]:
    """(표식을 지운 글, 숨긴 구간 [{start, end, reason}], 옛 위치 → 새 위치). 겹친 표식은 바깥 것만 쓰고, 짝 없는 표식은
    지운다(안 닫힌 시작은 글 끝까지 숨긴 글)."""
    if HS not in text and HE not in text:
        return text, [], lambda pos: pos
    pieces: list[str] = []
    spans: list[dict] = []
    cuts: list[int] = []  # 지운 표식(이유 코드 포함)의 옛 위치
    last = new_len = depth = start = 0
    reason = REASONS["h"]
    for m in _MARKS.finditer(text):
        i = m.start()
        if i < last:  # 이유 코드 자리에 온 표식 문자 — 이미 지웠다
            continue
        pieces.append(text[last:i])
        new_len += i - last
        if m.group() == HS:
            if depth == 0:
                start, reason = new_len, REASONS.get(text[i + 1:i + 2], REASONS["h"])
            depth += 1
            cuts += [i, i + 1]
            last = i + 2
        else:
            cuts.append(i)
            last = i + 1
            if depth:
                depth -= 1
                if depth == 0 and new_len > start:
                    spans.append({"start": start, "end": new_len, "reason": reason})
    pieces.append(text[last:])
    new_len += max(0, len(text) - last)
    if depth and new_len > start:
        spans.append({"start": start, "end": new_len, "reason": reason})
    return "".join(pieces), spans, lambda pos: pos - bisect_left(cuts, pos)


def invisible_spans(text: str, *, all_invisible: bool = False) -> list[dict]:
    """보이지 않는 유니코드 구간.

    기본은 패널에서 사람이 보는 확장·프록시 기준(폭 없는 문자 4개 이상)을 유지한다.
    all_invisible 은 MCP 전용으로 한 글자부터 잡되 정상 이모지의 ZWJ·VS16·국기 태그는 뺀다.
    """
    if all_invisible:
        return _all_invisible_spans(text)
    out = []
    for m in _ZERO_WIDTH.finditer(text):
        s, e = m.span()
        if s == 0 and text[0] == "﻿":
            s = 1
        if e - s >= 4:
            out.append({"start": s, "end": e, "reason": REASONS["u"]})
    for m in _ALWAYS.finditer(text):
        s, e = m.span()
        if m.group().endswith(_TAGS_END) and text[s - 1:s] == "\U0001f3f4" and all(c >= "\U000e0020" for c in m.group()):
            continue
        out.append({"start": s, "end": e, "reason": REASONS["u"]})
    return sorted(out, key=lambda x: x["start"])


def merge(spans: list[dict]) -> list[dict]:
    """겹치거나 맞닿은 구간을 하나로(앞 구간의 이유). 확장은 겹친 잠금 항목을 다른 유형으로 합친다."""
    out: list[dict] = []
    for s in sorted(spans, key=lambda x: (x["start"], -x["end"])):
        if out and s["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append(dict(s))
    return out


def _hex(color) -> str | None:
    """'FFFFFF'·'#ffffff'·'FFFFFFFF'(ARGB) → 'FFFFFF'. 모르면 None."""
    c = str(color or "").lstrip("#").upper()
    c = c[-6:] if len(c) == 8 else c
    return c if len(c) == 6 and all(ch in "0123456789ABCDEF" for ch in c) else None


def _luminance(hex6: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex6[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def color_reason(fg, bg) -> str | None:
    """글자색이 실제 배경과 거의 같으면(대비 1.1 미만 — 사람이 못 읽는다) 이유 코드, 아니면 None.
    모르는 색(테마 변형 등)은 숨기지 않은 것으로 본다 — 오탐보다 놓침 쪽."""
    fg, bg = _hex(fg), _hex(bg)
    if fg is None or bg is None:
        return None
    a, b = _luminance(fg), _luminance(bg)
    if (max(a, b) + 0.05) / (min(a, b) + 0.05) >= 1.1:
        return None
    return "w" if b > 0.8 else "c"
