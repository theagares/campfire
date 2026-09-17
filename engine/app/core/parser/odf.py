"""
app/core/parser/odf.py
OpenDocument 파싱 — ODT / ODS / ODP (PLAN §6).

**의존성을 추가하지 않는다.** ODF 는 OOXML 과 마찬가지로 ZIP 안에 XML 이 든 구조라
zipfile + xml.etree 로 충분하다. odfpy 를 넣어봐야 우리가 필요한 건 "텍스트 전부"
하나뿐이고, 그건 text: 네임스페이스의 노드를 훑으면 끝난다.

텍스트가 들어있는 자리:
  content.xml   본문 (셀·문단·표·도형 안의 글)
  styles.xml    머리말/꼬리말 — 여기에 회사명·문서번호·"대외비" 도장이 자주 있다
ODS(스프레드시트)는 셀이 전부 text:p 라 같은 방식으로 잡힌다.
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

# ODF 텍스트 네임스페이스. 문단·제목·목록항목·스팬이 전부 여기 있다.
_TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TABLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"

# 한 줄로 끊어야 하는 블록. 인라인 서식(text:span)만 이어 붙인다.
_BLOCK_TAGS = {_TEXT_NS + "p", _TEXT_NS + "h"}
_ROW_TAG = _TABLE_NS + "table-row"
_CELL_TAG = _TABLE_NS + "table-cell"


def _walk(node, out: list[str]) -> None:
    """블록 단위로 텍스트를 모은다."""
    if node.tag == _ROW_TAG:
        # 행을 통째로 itertext 하면 셀이 구분자 없이 붙는다("김철수kim@corp.co.kr").
        # 그러면 탐지기가 셀 경계를 잃어 이름+이메일이 한 값으로 보이거나, 숫자가
        # 이어붙어 엉뚱한 패턴이 된다. docx.py 와 같이 " | " 로 끊는다.
        cells = []
        for cell in node.iter(_CELL_TAG):
            text = "".join(cell.itertext()).strip()
            if text:
                cells.append(text)
        if cells:
            out.append(" | ".join(cells))
        return
    if node.tag in _BLOCK_TAGS:
        text = "".join(node.itertext()).strip()
        if text:
            out.append(text)
        return  # 블록 안은 itertext 로 이미 다 훑었다
    for child in node:
        _walk(child, out)


def extract_odf(file_bytes: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        names = set(z.namelist())
        # content 가 먼저, styles(머리말·꼬리말)가 뒤. 순서를 고정해야 좌표가 안정된다.
        for member in ("content.xml", "styles.xml"):
            if member not in names:
                continue
            try:
                root = ElementTree.fromstring(z.read(member))
            except ElementTree.ParseError:
                continue  # 한 파트가 깨져도 나머지는 살린다
            _walk(root, parts)
    return "\n".join(parts)
