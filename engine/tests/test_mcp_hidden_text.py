"""MCP 응답만 짧은 숨긴 글까지 전부 가리는 규칙. 모델 없이 검증한다."""

from __future__ import annotations

import asyncio

import pytest

from app import config
from app.adapters.http_api import jobs
from app.adapters.mcp import tools
from app.core import model_status


@pytest.fixture
def no_models(monkeypatch):
    """숨긴 글 규칙만 보며 PII·인젝션 모델 프로세스는 절대 올리지 않는다."""
    monkeypatch.setattr(model_status, "all_ready", lambda: False)
    monkeypatch.setattr(model_status, "pii_ready", lambda: False)
    monkeypatch.setattr(config, "INJECTION_POLICY", "mask")
    monkeypatch.setattr(config, "HIDDEN_TEXT_POLICY", "mask")


def test_mcp는_폭_없는_문자_한_개도_숨긴_글로_가린다(no_models):
    result = asyncio.run(tools.scan_text("안\u200b녕"))

    assert result["maskedText"] == "안[숨긴 글 마스킹]녕"
    assert [item["type"] for item in result["injectionItems"]] == ["HIDDEN_TEXT"]
    assert result["stats"]["injectionCount"] == 1


def test_확장_프롬프트는_기존의_네_글자_기준을_유지한다(no_models):
    response = asyncio.run(jobs.create_prompt_job("안\u200b녕"))

    assert response["result"]["maskedText"] == "안\u200b녕"
    assert response["result"]["injectionItems"] == []


def test_mcp는_빠지던_보이지_않는_문자와_변형_선택자를_한_묶음으로_가린다(no_models):
    hidden_run = (
        "\u00ad"       # soft hyphen
        "\u034f"       # combining grapheme joiner
        "\u115f\u1160"  # Hangul choseong/jungseong filler
        "\u3164\uffa0"  # Hangul filler / halfwidth Hangul filler
        "\u180e"       # Mongolian vowel separator
        "\ufe00"       # variation selector-1
        "\U000e0100"   # variation selector supplement
    )
    result = asyncio.run(tools.scan_text(f"앞{hidden_run}뒤"))

    assert result["maskedText"] == "앞[숨긴 글 마스킹]뒤"
    assert len(result["injectionItems"]) == 1


def test_mcp도_정상_이모지의_이음_선택자_국기_태그는_보존한다(no_models):
    emoji = "👨\u200d👩\u200d👧 ❤️ 🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"
    result = asyncio.run(tools.scan_text(emoji))

    assert result["maskedText"] == emoji
    assert result["injectionItems"] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("가\u200d나", "가[숨긴 글 마스킹]나"),
        ("가\ufe0f나", "가[숨긴 글 마스킹]나"),
        ("\u2764\ufe0f\ufe0f", "\u2764\ufe0f[숨긴 글 마스킹]"),
        ("🏴\U000e0067", "🏴[숨긴 글 마스킹]"),
    ],
)
def test_정상_이모지_모양이_아니면_예외로_우회할_수_없다(no_models, text, expected):
    result = asyncio.run(tools.scan_text(text))

    assert result["maskedText"] == expected
    assert [item["type"] for item in result["injectionItems"]] == ["HIDDEN_TEXT"]


def test_mask_text로_항목을_다시_골라도_숨긴_글은_남길_수_없다(no_models):
    result = asyncio.run(tools.mask_text("안\u200b녕", [], []))

    assert result["maskedText"] == "안[숨긴 글 마스킹]녕"
    assert [item["type"] for item in result["applied"]] == ["HIDDEN_TEXT"]


def test_mcp_경로와_파일명으로도_숨긴_글을_내보내지_않는다(no_models):
    assert tools._shown("report\u200b.txt") == "report[숨긴 글 마스킹].txt"


def test_mcp_파일_도구는_맨_앞_bom은_숨긴_글로_세지_않는다(
    no_models, monkeypatch, tmp_path
):
    monkeypatch.setattr(tools, "_PROJECT_ROOT", tmp_path.resolve())
    path = tmp_path / "bom.txt"
    path.write_bytes(b"\xef\xbb\xbf" + "정상 본문".encode())

    scanned = asyncio.run(tools.scan_file(str(path)))
    read = asyncio.run(tools.secure_read_file(str(path)))

    assert "[숨긴 글 마스킹]" not in scanned["maskedText"]
    assert "[숨긴 글 마스킹]" not in read["content"]


def test_mcp_파일_도구는_본문_중간_bom은_한_글자도_가린다(
    no_models, monkeypatch, tmp_path
):
    monkeypatch.setattr(tools, "_PROJECT_ROOT", tmp_path.resolve())
    path = tmp_path / "middle-bom.txt"
    path.write_text("앞\ufeff뒤", encoding="utf-8")

    scanned = asyncio.run(tools.scan_file(str(path)))
    read = asyncio.run(tools.secure_read_file(str(path)))

    assert scanned["maskedText"] == "앞[숨긴 글 마스킹]뒤"
    assert read["content"] == "앞[숨긴 글 마스킹]뒤"
