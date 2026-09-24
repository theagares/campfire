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

script/style 을 **정규식으로 먼저 걷어내고** 파서에는 건너뛰기 상태를 두지 않는다.
처음엔 파서 안에서 태그 깊이를 세어 건너뛰었는데, `<script>` 가 닫히지 않으면 깊이가
영원히 남아 **문서 전체가 빈 문자열로 나왔다**(실측). 그러면 파이프라인은 STATUS_OK
에 탐지 0건으로 끝난다 — 검사했다고 표시되지만 실제로는 아무것도 안 본, 이 코드베이스가
가장 경계하는 침묵 실패다. 닫히지 않은 script 의 내용이 본문에 섞이는 건 잡음일 뿐이고,
보안 게이트웨이에서 잡음은 침묵보다 낫다.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 온전히 닫힌 script/style 블록만 제거한다. 안 닫힌 것은 남겨서 잡음으로 흘린다.
_SCRIPTISH = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)

# 블록 요소 — 앞뒤로 줄을 넣어야 문장이 붙지 않는다.
_BLOCK = {
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "table",
}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def _brk(self, tag):
        if tag in _BLOCK:
            self.parts.append("\n")

    handle_starttag = lambda self, tag, attrs: self._brk(tag)  # noqa: E731
    handle_endtag = lambda self, tag: self._brk(tag)  # noqa: E731

    def handle_data(self, data):
        self.parts.append(data)


def strip_tags(text: str) -> str:
    """HTML 문자열 → 보이는 텍스트. eml.py 의 HTML 전용 메일도 이걸 쓴다."""
    p = _Extractor()
    try:
        p.feed(_SCRIPTISH.sub(" ", text))
        p.close()
    except Exception:  # noqa: BLE001 - 깨진 HTML 이어도 그때까지 모은 것은 쓴다
        pass
    lines = [ln.strip() for ln in "".join(p.parts).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_html(file_bytes: bytes) -> str:
    from .txt import extract_txt

    return strip_tags(extract_txt(file_bytes))
