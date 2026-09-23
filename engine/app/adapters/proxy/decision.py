"""
app/adapters/proxy/decision.py
검사 결과를 사람에게 보여주고 판단을 기다리는 자리 (HITL).

확장 프로그램 경로에서는 사이드패널이 이 역할을 했다. 프록시 경로에서는 요청을
**붙들고 있는 동안** 사람이 본다 — 브라우저는 그 사이 응답을 기다린다.

붙들 수 있는 시간에 상한이 있다는 걸 전제로 짰다(실측 근거는
`프록시_전환_비용.md` §5): 사이트 JS 의 abort 타임아웃은 우리가 정하는 값이
아니다. 그래서 여기서는 타임아웃을 **짧게 잡고 실패로 떨어뜨린다** — 오래
붙들다 사이트가 먼저 끊으면 사용자는 이유를 알 수 없는 업로드 실패를 본다.

fail-closed: 타임아웃도, 취소도, 예외도 전부 "보내지 않음" 이다. 판단이 서지
않았는데 원본을 흘려보내는 경로는 이 파일에 없다.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("securedoc.proxy.decision")

Action = Literal["send_masked", "send_original", "cancel"]


class DecisionTimeout(Exception):
    """사람이 제 시간에 답하지 않았다. 호출부는 fail-closed 로 처리한다."""


@dataclass
class PendingDecision:
    """검사까지 끝나고 사람 판단만 남은 한 건."""

    id: str
    file_name: str
    host: str
    result: dict[str, Any]
    future: asyncio.Future[Action] = field(repr=False)

    def summary(self) -> dict[str, Any]:
        """패널/CLI 가 목록에서 보여줄 최소 정보. 원문은 넣지 않는다."""
        stats = self.result.get("stats") or {}
        return {
            "id": self.id,
            "fileName": self.file_name,
            "host": self.host,
            "piiCount": stats.get("piiCount", 0),
            "injectionCount": stats.get("injectionCount", 0),
            "blocked": bool(self.result.get("blocked")),
            "scanStatus": self.result.get("scanStatus"),
        }


class DecisionBroker:
    """보류 중인 판단들의 보관소.

    프록시 애드온과 http_api 가 **같은 프로세스·같은 이벤트 루프**에 있기 때문에
    이 객체 하나를 공유하면 끝이다. 요청을 붙드는 쪽은 future 를 await 하고,
    사람이 누르는 쪽은 같은 future 를 resolve 한다 — IPC 도 폴링도 없다.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingDecision] = {}

    def list_pending(self) -> list[dict[str, Any]]:
        return [p.summary() for p in self._pending.values()]

    def get(self, decision_id: str) -> PendingDecision | None:
        return self._pending.get(decision_id)

    def detail(self, decision_id: str) -> dict[str, Any] | None:
        """판단에 필요한 전체 정보. 원문/마스킹본 diff 가 여기 들어간다."""
        p = self._pending.get(decision_id)
        if p is None:
            return None
        return {**p.summary(), "result": p.result}

    async def wait(
        self,
        *,
        file_name: str,
        host: str,
        result: dict[str, Any],
        timeout_s: float,
    ) -> Action:
        """판단이 날 때까지 기다린다. 이 await 동안 브라우저 요청이 붙들려 있다.

        타임아웃·취소는 예외로 나간다 — 호출부가 통과시킬 수 없게 하려는 것이다.
        조용히 기본값을 돌려주면 그게 곧 게이트웨이가 꺼지는 실패 모드가 된다
        (확장 경로에서 promptApproved 가 true 로 남아 있던 사고와 같은 모양).
        """
        decision_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        pending = PendingDecision(
            id=decision_id,
            file_name=file_name,
            host=host,
            result=result,
            future=loop.create_future(),
        )
        self._pending[decision_id] = pending
        logger.info(
            "[proxy] 판단 대기 id=%s file=%s host=%s pii=%d injection=%d",
            decision_id,
            file_name,
            host,
            (result.get("stats") or {}).get("piiCount", 0),
            (result.get("stats") or {}).get("injectionCount", 0),
        )
        try:
            return await asyncio.wait_for(pending.future, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            logger.warning("[proxy] 판단 타임아웃 id=%s (%.0fs)", decision_id, timeout_s)
            raise DecisionTimeout(
                f"{timeout_s:.0f}초 안에 판단이 없었습니다"
            ) from exc
        finally:
            self._pending.pop(decision_id, None)

    def cancel_all(self, reason: str = "프록시가 중지됐습니다") -> int:
        """붙들고 있는 판단을 전부 끝낸다. 끝낸 개수를 돌려준다.

        프록시를 끌 때 부른다. 안 부르면 브라우저 요청이 사라진 루프 위에서
        타임아웃까지 매달린다. 예외로 끝내므로 애드온은 차단(403)으로 떨어진다 —
        끄는 중에 원본이 나가는 경로는 없다.
        """
        n = 0
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(DecisionTimeout(reason))
                n += 1
        return n

    def resolve(self, decision_id: str, action: Action) -> bool:
        """사람이 눌렀다. 성공하면 True.

        이미 끝난 건(타임아웃으로 사라졌거나 두 번 눌렀거나)은 False 다 —
        호출부가 404/409 로 구분해 보여줄 수 있게 예외를 쓰지 않는다.
        """
        pending = self._pending.get(decision_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(action)
        return True


# 프로세스 전역 하나. 애드온과 http_api 가 같은 객체를 봐야 한다.
broker = DecisionBroker()
