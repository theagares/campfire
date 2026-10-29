"""프록시가 사람에게 묻지 않고 막은 전송은 데스크톱 알림으로 알린다 — 사이트는 이유 없이 "업로드 실패" 만 보여 준다.

맥 v0.2.58: ChatGPT 그림이 막혀 칩이 "업로드 실패", 전송 버튼이 꺼졌는데 이유 안내가 없었다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow, tutils  # noqa: E402

import app.adapters.proxy.addon as addon_mod  # noqa: E402
from app.adapters.proxy.addon import CampfireAddon  # noqa: E402
from app.adapters.proxy.decision import DecisionBroker  # noqa: E402


@pytest.fixture
def broker(monkeypatch):
    b = DecisionBroker()
    monkeypatch.setattr(addon_mod, "broker", b)
    return b


def _flow(host="sdmntprjapaneast.oaiusercontent.com"):
    return tflow.tflow(req=tutils.treq(method="PUT", host=host, path="/files/x/raw", content=b"x"))


def test_막은_이유를_사이트_이름과_함께_알린다(broker):
    v = broker.version
    CampfireAddon()._block(_flow(), "검사를 끝내지 못한 그림이라 보내지 않았습니다")
    [n] = broker.list_notices()
    assert (n["host"], n["reason"]) == ("chatgpt.com", "검사를 끝내지 못한 그림이라 보내지 않았습니다")
    assert broker.version > v, "데스크톱의 장기 대기를 깨워야 바로 알린다"


def test_사람이_취소한_건과_검토_시간_초과는_알리지_않는다(broker):
    a = CampfireAddon()
    a._block(_flow(), addon_mod._CANCELLED)
    a._block(_flow(), "검토 시간이 지나 전송을 취소했습니다 (120초)", notify=False)
    assert broker.list_notices() == []


def test_검사_단계의_구체적인_이유가_있으면_그걸_알린다(broker):
    f = _flow("claude.ai")
    f.metadata["campfire_block_reason"] = "첨부한 파일(a_masked.md)에 프롬프트 인젝션이 있어 전송을 막았습니다"
    CampfireAddon()._block(f, addon_mod._CANCELLED)
    assert [n["reason"] for n in broker.list_notices()] == [f.metadata["campfire_block_reason"]]
    assert b"a_masked.md" in f.response.content


def test_사이트의_재시도로_같은_이유가_잇따르면_한_번만_알린다(broker):
    a = CampfireAddon()
    for _ in range(3):
        a._block(_flow(), "업로드 세션을 확인하지 못해 전송을 막았습니다")
    assert len(broker.list_notices()) == 1
