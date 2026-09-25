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
"""

from __future__ import annotations

from html.parser import HTMLParser

# 문맥으로 건너뛸 요소. 내용이 화면에 안 보이고 토큰만 오염시킨다.
_SKIP = {"script", "style"}

# 블록 요소 — 앞뒤로 줄을 넣어야 문장이 붙지 않는다.
_BLOCK = {
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "table",
}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0             # script/style 안에 있는 깊이
        self._buf: list[str] = []  # 건너뛴 내용 — 안 닫히면 방출한다

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            if self._skip:
                self._skip -= 1
                if self._skip == 0:
                    self._buf.clear()  # 제대로 닫힌 블록의 내용은 버린다
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        # script/style 안이면 버퍼로. 닫히면 버려지고, 안 닫히면 close 에서 방출된다.
        (self._buf if self._skip else self.parts).append(data)

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
