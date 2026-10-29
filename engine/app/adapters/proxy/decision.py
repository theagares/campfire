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
import itertools
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("securedoc.proxy.decision")

Action = Literal["send_masked", "send_original", "cancel"]

# 판단을 기다리는 동안 브라우저 연결이 살아 있는지 보는 간격.
_ALIVE_CHECK_S = 1.0


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
            "forcedMaskCount": stats.get("forcedMaskCount", len(self.result.get("forcedMaskItems") or [])),
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
        # 데스크톱은 판단 목록을 짧은 주기로 폴링하지 않고 이 버전의 변화를 장기 대기한다.
        # waiter 는 요청별 Future 라서 한 소비자가 깨어나도 다른 소비자의 알림을 뺏지 않는다.
        self._version = 0
        self._change_waiters: set[asyncio.Future[int]] = set()
        # 프록시가 사람에게 묻지 않고 막은 전송(미지원·검사 실패·첨부 인젝션 등)의 이유. 사이트는 이유 없이 "업로드
        # 실패" 만 보여 주므로(맥 v0.2.58: ChatGPT 그림 칩 "업로드 실패", 안내 없음) 데스크톱이 같은 장기 대기로 받아
        # 알림으로 띄운다. 원문은 싣지 않는다 — 이유 문구와 호스트뿐이다.
        self._notices: deque[dict[str, Any]] = deque(maxlen=20)
        self._notice_ids = itertools.count(1)

    @property
    def version(self) -> int:
        return self._version

    def notify(self, host: str, reason: str) -> None:
        """사람에게 묻지 않고 막은 전송을 알린다. 같은 이유가 잇따르면(사이트의 업로드 재시도) 한 번만 남긴다."""
        last = self._notices[-1] if self._notices else None
        now = time.time()
        if last and last["host"] == host and last["reason"] == reason and now - last["at"] < 10:
            return
        self._notices.append({"id": next(self._notice_ids), "host": host, "reason": reason, "at": now})
        self._notify_change()

    def list_notices(self) -> list[dict[str, Any]]:
        return list(self._notices)

    def _notify_change(self) -> None:
        self._version += 1
        for waiter in tuple(self._change_waiters):
            if not waiter.done():
                waiter.set_result(self._version)

    async def wait_for_change(self, after: int, timeout_s: float) -> int:
        """목록 버전이 ``after``와 달라질 때까지 기다린다.

        먼저 버전을 비교하고 waiter 를 등록한 뒤 한 번 더 비교해, 그 사이에 들어온
        판단을 놓치는 check-then-wait 경쟁을 막는다. 타임아웃은 정상적인 keepalive다.
        """
        if after != self._version:
            return self._version
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[int] = loop.create_future()
        self._change_waiters.add(waiter)
        if after != self._version and not waiter.done():
            waiter.set_result(self._version)
        try:
            return await asyncio.wait_for(waiter, timeout=max(0.0, timeout_s))
        except asyncio.TimeoutError:
            return self._version
        finally:
            self._change_waiters.discard(waiter)

    def list_pending(self) -> list[dict[str, Any]]:
        return [p.summary() for p in self._pending.values() if not p.future.done()]

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
        is_alive: Callable[[], bool] | None = None,
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
        self._notify_change()
        logger.info(
            "[proxy] 판단 대기 id=%s host=%s pii=%d injection=%d forced=%d",
            decision_id,
            host,
            (result.get("stats") or {}).get("piiCount", 0),
            (result.get("stats") or {}).get("injectionCount", 0),
            (result.get("stats") or {}).get("forcedMaskCount", len(result.get("forcedMaskItems") or [])),
        )
        try:
            if is_alive is None:
                return await asyncio.wait_for(pending.future, timeout=timeout_s)
            return await self._wait_while_alive(decision_id, pending.future, timeout_s, is_alive)
        except asyncio.TimeoutError as exc:
            logger.warning("[proxy] 판단 타임아웃 id=%s (%.0fs)", decision_id, timeout_s)
            raise DecisionTimeout(
                f"{timeout_s:.0f}초 안에 판단이 없었습니다"
            ) from exc
        finally:
            if not pending.future.done():
                pending.future.cancel()  # 늦게 온 판단은 resolve 가 409 로 돌려보낸다
            if self._pending.pop(decision_id, None) is not None:
                self._notify_change()

    @staticmethod
    async def _wait_while_alive(
        decision_id: str, future: asyncio.Future, timeout_s: float, is_alive: Callable[[], bool]
    ) -> Action:
        """판단을 기다리되, 브라우저가 연결을 끊으면 그 자리에서 끝낸다.

        mitmproxy 는 클라이언트가 끊겨도 기다리는 훅을 취소하지 않는다. 그대로 두면 이미 없는
        요청의 판단 건이 타임아웃(기본 120초)까지 목록에 남아, 앱이 그 확인창을 계속 띄웠다
        (맥 실측: 탭을 옮긴 뒤 "취소" 해도 같은 건이 다시 떴다). 끝낼 때는 차단과 같다 —
        원본을 내보내는 경로가 아니다.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=min(_ALIVE_CHECK_S, remaining)
                )
            except asyncio.TimeoutError:
                if not is_alive():
                    logger.info("[proxy] 판단 대기 중 브라우저 연결이 끊김 id=%s", decision_id)
                    raise DecisionTimeout("브라우저 요청이 끊겼습니다") from None

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
        if n:
            self._notify_change()
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
        # wait()의 finally가 dict에서 지우기 전이라도 목록에서는 future.done()인 항목을
        # 제외한다. 따라서 UI는 이 알림을 받는 즉시 판단이 사라졌다고 볼 수 있다.
        self._notify_change()
        return True


# 프로세스 전역 하나. 애드온과 http_api 가 같은 객체를 봐야 한다.
broker = DecisionBroker()
