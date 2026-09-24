"""판단 엔드포인트 — 프록시가 붙든 요청에 사람이 답하는 통로.

lifespan 은 돌리지 않는다(모델 로드가 붙는다). 라우팅과 상태 코드만 본다.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import app.main as main_mod
from app.adapters.proxy.decision import broker


def _client() -> TestClient:
    return TestClient(main_mod.app, raise_server_exceptions=False)


def test_대기_목록은_기본이_비어있다():
    r = _client().get("/decisions")
    assert r.status_code == 200
    assert r.json() == {"pending": []}


def test_없는_id_는_409_이지_500_이_아니다():
    """UI 가 "이미 처리됨" 과 서버 오류를 구분할 수 있어야 한다."""
    r = _client().post("/decisions/없는id", json={"action": "send_masked"})
    assert r.status_code == 409


def test_모르는_action_은_400():
    r = _client().post("/decisions/아무거나", json={"action": "rm -rf"})
    assert r.status_code == 400


def test_대기_중인_건이_목록과_상세에_보인다():
    """broker 는 프록시 애드온과 http_api 가 공유하는 같은 객체다 —
    애드온이 붙든 건이 REST 로 그대로 보여야 HITL 이 성립한다."""

    async def go():
        task = asyncio.create_task(
            broker.wait(
                file_name="계약서.docx",
                host="claude.ai",
                result={
                    "maskedText": "# masked",
                    "originalText": "주민번호 900101-1234567",
                    "stats": {"piiCount": 1, "injectionCount": 0},
                    "scanStatus": "ok",
                },
                timeout_s=5.0,
            )
        )
        await asyncio.sleep(0)

        c = _client()
        listed = c.get("/decisions").json()["pending"]
        assert len(listed) == 1
        assert listed[0]["fileName"] == "계약서.docx"
        assert listed[0]["piiCount"] == 1
        # 목록에 원문이 새어 나오면 안 된다.
        assert "900101" not in str(listed)

        did = listed[0]["id"]
        detail = c.get(f"/decisions/{did}").json()
        assert detail["result"]["maskedText"] == "# masked"

        assert c.post(f"/decisions/{did}", json={"action": "cancel"}).status_code == 200
        assert await task == "cancel"
        # 끝난 뒤에는 상세도 사라진다.
        assert c.get(f"/decisions/{did}").status_code == 404

    asyncio.run(go())
