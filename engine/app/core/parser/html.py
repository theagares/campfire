"""
app/core/parser/html.py
HTML 파싱 — .html / .htm (PLAN §6). 표준 라이브러리 html.parser 만 쓴다.

지금도 HTML 은 _looks_textual 폴백을 타고 "검사됨" 으로 통과하지만, 그때 탐지기가
보는 것은 태그와 스크립트가 뒤섞인 원본이다. 그러면 두 방향으로 다 틀린다:
  - script/style 안의 토큰이 탐지기 입력을 오염시킨다(오탐)
  - 태그가 문장을 끊어 "홍<b>길동</b>" 같은 값이 한 덩어리로 안 잡힌다(미탐)
그래서 폴백에 맡기지 않고 태그를 벗겨 본문만 넘긴다.

BeautifulSoup 를 쓰지 않는 이유: 우리가 필요한 건 "보이는 텍스트" 하나뿐이고
HTMLParser 로 충분하다. 파서 의존성 하나를 더 들일 값어치가 없다.

script/style 은 **파서의 문맥**으로 건너뛴다. 한때 정규식(`<script>.*?</script>`)으로
먼저 지웠는데, 그 방식은 주석(`<!-- <script> -->`)·속성값(`title="<script>"`)·
textarea 안의 **리터럴** `<script>` 까지 여는 태그로 보고 lazy `.*?` 로 다음 진짜
`</script>` 까지 삼켰다. 그 사이의 **보이는 텍스트가 통째로 사라져** 스캐너가 빈
문자열을 보고 STATUS_OK·탐지 0건으로 끝났다 — 이 코드베이스가 가장 경계하는 침묵
검사 우회다. 게다가 안 닫힌 `<script` 가 많으면 매 opener 가 EOF 까지 다시 훑어
O(n²) 가 돼(≈1MB 에서 30초 초과) 파싱 타임아웃 → 미검사 통과로 떨어졌다.
HTMLParser 는 주석·속성·textarea 를 문맥으로 구분하므로(실측) 두 문제가 다 사라지고,
토큰화는 선형이다.

안 닫힌 script/style 로 문서가 통째로 비는 것(예전에 파서 안에서 태그 깊이만 세다
`<script>` 가 안 닫히면 깊이가 영원히 남아 전부 빈 문자열이 됐던 그 버그)은, 건너뛴
내용을 버퍼에 담아 두었다가 닫히지 않은 채 끝나면 본문으로 방출해 막는다 — 보안
게이트웨이에서 잡음은 침묵보다 낫다.

숨긴 글(parser/hidden.py): hidden 속성과 인라인 style 의 display:none·visibility:hidden·opacity:0·color:transparent,
font-size 1 이하, 화면 밖 위치(absolute·fixed 로 -500px 밖, text-indent -500px 밖), 글자색 = 배경색(조상 요소의
background·bgcolor, 없으면 흰색)인 요소의 글을 표식으로 감싼다. <style> 의 클래스 규칙·외부 CSS 는 보지 않는다(브라우저 없이는
계산할 수 없다).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from .hidden import color_reason, wrap

# 문맥으로 건너뛸 요소. 내용이 화면에 안 보이고 토큰만 오염시킨다.
_SKIP = {"script", "style"}

# 블록 요소 — 앞뒤로 줄을 넣어야 문장이 붙지 않는다.
_BLOCK = {
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "table",
}
# 닫는 태그가 없는 요소 — 숨김 문맥 스택에 쌓지 않는다.
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_NAMED = {"white": "FFFFFF", "black": "000000", "red": "FF0000", "blue": "0000FF", "green": "008000",
          "yellow": "FFFF00", "gray": "808080", "grey": "808080", "silver": "C0C0C0", "navy": "000080"}
_NUM = re.compile(r"^(-?\d+(?:\.\d+)?)(px|pt|em|rem|%)?$")


def _css_color(value: str | None) -> str | None:
    """CSS 색 → RGB 16진, 투명이면 'transparent', 모르면 None."""
    v = (value or "").strip().lower().replace(" ", "")
    if v == "transparent":
        return v
    if m := re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", v):
        h = m.group(1)
        return ("".join(c * 2 for c in h) if len(h) == 3 else h).upper()
    if m := re.fullmatch(r"rgba?\((\d+),(\d+),(\d+)(?:,([\d.]+))?\)", v):
        if m.group(4) is not None and float(m.group(4)) == 0:
            return "transparent"
        return "".join(f"{min(255, int(x)):02X}" for x in m.groups()[:3])
    return _NAMED.get(v)


def _px(value: str | None) -> float | None:
    m = _NUM.match((value or "").strip().lower())
    return float(m.group(1)) if m else None


def _style(attrs: dict) -> dict[str, str]:
    out = {}
    for decl in (attrs.get("style") or "").split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip().lower()] = v.strip().lower().replace("!important", "").strip()
    return out


def _element_code(attrs: dict, style: dict, bg: str | None) -> str | None:
    if "hidden" in attrs or style.get("display") == "none" or style.get("visibility") in ("hidden", "collapse"):
        return "h"
    if (opacity := _px(style.get("opacity"))) is not None and opacity <= 0.01:
        return "h"
    if (size := _px(style.get("font-size"))) is not None and size <= 1:
        return "s"
    if style.get("position") in ("absolute", "fixed") and any(
            (v := _px(style.get(k))) is not None and v <= -500 for k in ("left", "top", "right")):
        return "o"
    if (indent := _px(style.get("text-indent"))) is not None and indent <= -500:
        return "o"
    color = _css_color(style.get("color") or attrs.get("color"))
    if color == "transparent":
        return "h"
    return color_reason(color, bg)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0             # script/style 안에 있는 깊이
        self._buf: list[str] = []  # 건너뛴 내용 — 안 닫히면 방출한다
        self._stack: list[tuple[str, str | None, str | None]] = []  # (요소, 숨긴 이유, 배경색)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        if tag in _SKIP or tag in _VOID:
            return
        if tag == "p" and self._stack and self._stack[-1][0] == "p":
            self._stack.pop()  # 안 닫은 <p> — 브라우저처럼 다음 <p> 가 닫는다
        a = {k.lower(): (v or "") for k, v in attrs}
        style = _style(a)
        parent_code, parent_bg = self._stack[-1][1:] if self._stack else (None, "FFFFFF")
        own_bg = _css_color(style.get("background-color") or style.get("background", "").split(" ")[0] or a.get("bgcolor"))
        bg = parent_bg if own_bg in (None, "transparent") else own_bg
        self._stack.append((tag, parent_code or _element_code(a, style, bg), bg))

    def handle_endtag(self, tag):
        if tag in _SKIP:
            if self._skip:
                self._skip -= 1
                if self._skip == 0:
                    self._buf.clear()  # 제대로 닫힌 블록의 내용은 버린다
        elif tag in _BLOCK:
            self.parts.append("\n")
        for i in range(len(self._stack) - 1, -1, -1):  # 짝이 맞는 요소까지 닫는다(안 닫은 자식 포함)
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        # script/style 안이면 버퍼로. 닫히면 버려지고, 안 닫히면 close 에서 방출된다.
        if self._skip:
            self._buf.append(data)
            return
        code = self._stack[-1][1] if self._stack else None
        core = data.strip()
        if code and core:  # 앞뒤 공백은 밖에 둔다 — 줄 정리(strip_tags) 뒤 표식만 남은 줄이 생기지 않게
            start = data.index(core)
            data = data[:start] + wrap(core, code) + data[start + len(core):]
        self.parts.append(data)

    def close(self):
        try:
            super().close()
        finally:
            if self._skip and self._buf:
                # 안 닫힌 script/style — 내용을 잃지 않고 본문으로 흘린다(잡음 > 침묵).
                self.parts.extend(self._buf)
                self._buf.clear()
                self._skip = 0


def strip_tags(text: str) -> str:
    """HTML 문자열 → 보이는 텍스트. eml.py 의 HTML 전용 메일도 이걸 쓴다."""
    p = _Extractor()
    try:
        p.feed(text)
    except Exception:  # noqa: BLE001 - 깨진 HTML 이어도 그때까지 모은 것은 쓴다
        pass
    try:
        p.close()  # close 가 finally 로 미방출 버퍼(안 닫힌 블록)를 흘린다
    except Exception:  # noqa: BLE001
        pass
    lines = [ln.strip() for ln in "".join(p.parts).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_html(file_bytes: bytes) -> str:
    from .txt import extract_txt

    return strip_tags(extract_txt(file_bytes))
