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
"""

from __future__ import annotations

from html.parser import HTMLParser

# 안에 든 글자가 화면에 안 보이는 태그. 내용을 통째로 버린다.
_SKIP = {"script", "style", "noscript", "template", "head"}
# 블록 요소 — 앞뒤로 줄을 넣어야 문장이 붙지 않는다.
_BLOCK = {
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "table",
}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def strip_tags(text: str) -> str:
    """HTML 문자열 → 보이는 텍스트. eml.py 의 HTML 전용 메일도 이걸 쓴다."""
    p = _Extractor()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001 - 깨진 HTML 이어도 그때까지 모은 것은 쓴다
        pass
    lines = [ln.strip() for ln in "".join(p.parts).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_html(file_bytes: bytes) -> str:
    from .txt import extract_txt

    return strip_tags(extract_txt(file_bytes))
