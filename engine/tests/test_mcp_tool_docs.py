"""MCP 도구 설명(AI 클라이언트에 보이는 docstring)이 실제 지원 형식과 맞는지 (app/adapters/mcp/tools.py).

scan_file 설명에 「TXT/PDF/DOCX 지원, HWP·XLSX·PPTX 는 v1 미지원」이 남아 있어 AI 가 지원 형식을 검사하지 않고
넘기려 했다. config 에 형식을 더하면 설명도 같이 고치도록 여기서 맞춰 본다.
"""

import asyncio
import re

from app import config
from app.adapters.mcp import tools


def _description(name: str) -> str:
    return next(t.description for t in asyncio.run(tools.mcp.list_tools()) if t.name == name)


def test_scan_file_description_lists_real_formats():
    desc = _description("scan_file")
    assert "미지원" not in desc and "TXT/PDF/DOCX" not in desc
    named = set(re.findall(r"\.[a-z0-9]+", desc))
    expected = config.SUPPORTED_EXTENSIONS | config.UNSUPPORTED_EXTENSIONS | config.IMAGE_OCR_EXTENSIONS
    assert expected <= named, sorted(expected - named)
