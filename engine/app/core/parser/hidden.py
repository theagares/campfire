"""
app/core/parser/hidden.py
사람 눈에 안 보이게 숨긴 글 — 인젝션 모델 판정과 무관하게 규칙으로 가리고 잠근다(사용자 결정 2026-10-29).

파서는 숨긴 구간을 표식(사용 영역 문자 HS + 이유 코드 한 글자 + 글 + HE)으로 감싸 본문에 남긴다 — 그림 표시(images.py)와
같은 방식. orchestrator 가 extract() 로 표식을 지우고 구간을 HIDDEN_TEXT 항목으로 바꾼다. 보이지 않는 유니코드(폭 없는
문자·태그 문자·방향 제어)는 형식과 관계없이 모든 글(프롬프트 포함)에서 invisible_spans() 로 찾는다.
오탐을 줄이려고 사람이 정말 못 보는 경우만 잡는다 — 글자색은 실제 배경과 비교한다(어두운 칸의 흰 글씨는 보인다).
"""

from __future__ import annotations

import re
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
# 폭 없는 문자 U+200B–200F·2060–2064·FEFF, 방향 제어 U+202A–202E·2066–2069, 태그 문자 U+E0000–E007F(ASCII smuggling)
_INVISIBLE = re.compile("[​-‏⁠-⁤﻿‪-‮⁦-⁩\U000e0000-\U000e007f]+")
_TAGS_END = "\U000e007f"


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


def _pictograph(ch: str) -> bool:
    return bool(ch) and (ord(ch) >= 0x2600 or ch == "️")


def invisible_spans(text: str) -> list[dict]:
    """보이지 않는 유니코드 구간. 글 맨 앞 BOM, 이모지를 잇는 U+200D(👨‍👩‍👧), 지역 깃발(🏴 + 태그 문자)은 뺀다."""
    out = []
    for m in _INVISIBLE.finditer(text):
        s, e = m.span()
        if s == 0 and text[0] == "﻿":
            s = 1
        run = text[s:e]
        if not run:
            continue
        if run == "‍" and _pictograph(text[s - 1:s]) and _pictograph(text[e:e + 1]):
            continue
        if run.endswith(_TAGS_END) and text[s - 1:s] == "\U0001f3f4" and all(c >= "\U000e0020" for c in run):
            continue
        out.append({"start": s, "end": e, "reason": REASONS["u"]})
    return out


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
