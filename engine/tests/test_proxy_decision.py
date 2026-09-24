"""HITL 보류 — 여기서 틀리면 게이트웨이가 조용히 꺼진다.

확장 경로에서 `promptApproved` 가 true 로 남아 검사 없이 전송되던 사고가 있었다.
같은 모양의 실패(판단이 안 났는데 통과)가 프록시에서 나지 않는지를 본다.

레포 관례대로 asyncio.run() 으로 루프를 직접 연다(tests/conftest.py 참고).
"""

from __future__ import annotations

import asyncio

import pytest

from app.adapters.proxy.decision import DecisionBroker, DecisionTimeout

RESULT = {"maskedText": "# m", "stats": {"piiCount": 2, "injectionCount": 0}, "scanStatus": "ok"}


def _wait(broker: DecisionBroker, timeout_s: float = 5.0, result: dict | None = None):
    return broker.wait(
        file_name="a.docx", host="claude.ai", result=result or RESULT, timeout_s=timeout_s
    )


def test_사람이_누르면_그_값이_나온다():
    async def go():
        broker = DecisionBroker()
        task = asyncio.create_task(_wait(broker))
        await asyncio.sleep(0)  # 등록될 틈을 준다
        pending = broker.list_pending()
        assert len(pending) == 1
        assert pending[0]["piiCount"] == 2

        assert broker.resolve(pending[0]["id"], "send_masked") is True
        assert await task == "send_masked"

    asyncio.run(go())


def test_타임아웃은_예외다_통과가_아니다():
    """조용히 기본값을 돌려주면 그게 곧 원본 유출이다."""

    async def go():
        broker = DecisionBroker()
        with pytest.raises(DecisionTimeout):
            await _wait(broker, timeout_s=0.05)
        # 끝난 건은 목록에서 사라져야 한다 — 남아 있으면 UI 가 유령 항목을 그린다.
        assert broker.list_pending() == []

    asyncio.run(go())


def test_두_번_누르면_두_번째는_거절된다():
    async def go():
        broker = DecisionBroker()
        task = asyncio.create_task(_wait(broker))
        await asyncio.sleep(0)
        did = broker.list_pending()[0]["id"]

        assert broker.resolve(did, "cancel") is True
        assert broker.resolve(did, "send_original") is False  # 이미 끝났다
        assert await task == "cancel"

    asyncio.run(go())


def test_없는_id_는_조용히_False():
    broker = DecisionBroker()
    assert broker.resolve("없는id", "send_masked") is False


def test_목록에는_원문이_없다():
    """대기 목록은 여러 곳에 뿌려질 수 있다. 원문이 따라다니면 안 된다."""

    async def go():
        broker = DecisionBroker()
        task = asyncio.create_task(
            _wait(broker, result={**RESULT, "originalText": "주민번호 900101-1234567"})
        )
        await asyncio.sleep(0)
        summary = broker.list_pending()[0]
        assert "originalText" not in summary
        assert "900101" not in str(summary)
        # 상세에는 있어야 한다 — 검토 화면이 diff 를 그려야 하므로.
        assert "originalText" in broker.detail(summary["id"])["result"]

        broker.resolve(summary["id"], "cancel")
        await task

    asyncio.run(go())
