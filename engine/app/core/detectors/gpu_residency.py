"""
app/core/detectors/gpu_residency.py
GPU 상주 정책 실제 구현 (PLAN §4.1, Phase 6).

모델별 상주 방식:
    - "always_on"  : PII 인코더. 생성 즉시 로드 완료 상태로 취급, idle 언로드 없음.
    - "idle_unload": 인젝션 LLM. 마지막 사용(touch) 후 idle_timeout_sec 이 지나면
      "unloaded" 로 전환. unloaded 상태에서 ensure_loaded() 를 호출하면 검사를
      생략하지 않고 load_delay_sec 만큼 대기(fail-closed)한 뒤 "loaded" 로 전환한다.

실제 GPU 메모리는 전혀 다루지 않는다 — 상태 플래그(state)와 타이머(마지막 사용
시각 대비 경과 시간)만으로 정책을 흉내낸다. 이 클래스는 encoder.py/llm_mcp.py
스텁이 공유하며, registry.residency_status() 를 통해 /health, get_status(MCP) 에서
조회 가능하다.

동시성: 언로드 상태에서 여러 요청이 동시에 들어와도 실제 "로드"가 한 번만
일어나도록 asyncio.Lock 으로 보호한다(락 대기 중 다른 요청이 이미 로드를 끝냈으면
추가 대기 없이 바로 통과).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

ResidencyMode = Literal["always_on", "idle_unload"]
ResidencyState = Literal["loaded", "unloaded", "loading"]


@dataclass
class GpuResidency:
    model_name: str
    mode: ResidencyMode
    idle_timeout_sec: float | None = None
    load_delay_sec: float = 0.0

    state: ResidencyState = field(init=False, default="unloaded")
    last_used_at: float = field(init=False, default=0.0)
    load_count: int = field(init=False, default=0)
    # 서브프로세스 준비(ready) 응답의 "device"(cuda/mps/cpu)를 그대로 받아 채운다 —
    # GPU 가 안 잡힌다는 리포트가 실측 방법 자체가 없었어서(엔진이 디바이스 선택을
    # 어디에도 노출하지 않음), /health 로 바로 확인 가능하게 한다.
    device: str | None = field(init=False, default=None)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock, repr=False)

    def mark_loaded_immediately(self) -> None:
        """always_on 모델을 생성 시점에 즉시 "로드 완료" 상태로 만든다."""
        self.state = "loaded"
        self.last_used_at = time.monotonic()

    def _check_idle_expiry(self) -> None:
        """idle_unload 모드에서 마지막 사용 후 idle timeout 이 지났으면 unloaded 로 전환.

        타이머는 실시간(time.monotonic()) 경과분을 매 조회 시점에 평가하는 방식으로
        구현했다(별도 백그라운드 태스크 없이도 상태 조회/사용 시점마다 정확히 반영됨).
        """
        if self.mode != "idle_unload" or self.state != "loaded":
            return
        if self.idle_timeout_sec is None:
            return
        if time.monotonic() - self.last_used_at >= self.idle_timeout_sec:
            self.state = "unloaded"

    async def ensure_loaded(self) -> None:
        """fail-closed 로드 대기: unloaded 상태면 검사를 생략하지 않고 로드 완료까지 대기한다."""
        self._check_idle_expiry()
        if self.state == "loaded":
            return

        async with self._lock:
            # 락 대기 중 다른 요청이 이미 로드를 끝냈을 수 있으므로 재확인.
            self._check_idle_expiry()
            if self.state == "loaded":
                return
            self.state = "loading"
            await asyncio.sleep(self.load_delay_sec)
            self.state = "loaded"
            self.last_used_at = time.monotonic()
            self.load_count += 1

    def touch(self) -> None:
        """사용 시점 기록 — idle timeout 타이머를 리셋한다."""
        self.last_used_at = time.monotonic()

    @property
    def status(self) -> dict[str, Any]:
        self._check_idle_expiry()
        idle_for = time.monotonic() - self.last_used_at if self.last_used_at else None
        return {
            "model": self.model_name,
            "mode": self.mode,
            "state": self.state,
            "idleTimeoutSec": self.idle_timeout_sec,
            "idleForSec": round(idle_for, 3) if idle_for is not None else None,
            "loadCount": self.load_count,
            "device": self.device,
        }
