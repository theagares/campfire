"""
app/core/detectors/confidential/retriever.py
기밀 탐지 리트리버 하위 프로세스 (runtime/local_retriever_inference.py).

PII 인코더와 같은 JSONL 표준입출력 프로토콜이라 프로세스 수명 관리(준비 대기·시간 상한·
멎으면 재시작·요청 직렬화)를 그대로 물려받는다. KG 가 설정되고 후보가 처음 생길 때만 띄운다.
"""

from __future__ import annotations

from typing import Any

from app import config

from ..pii.encoder import EncoderPiiDetector


class RetrieverProcess(EncoderPiiDetector):
    name = "confidential_retriever"
    kind = "confidential"

    def _spawn_args(self) -> list[str]:
        return [
            config.PII_PYTHON_EXECUTABLE,
            str(config.CONFIDENTIAL_ENGINE_DIR / "runtime" / "local_retriever_inference.py"),
            "--model-dir", str(config.CONFIDENTIAL_ENGINE_DIR / "models"),
            "--device", config.CONFIDENTIAL_DEVICE,
            "--max-length", str(config.CONFIDENTIAL_MAX_LENGTH),
            "--stdio",
        ]

    async def _warmup(self) -> None:
        return None

    async def detect(self, text: str, *, meta=None):  # noqa: D102 - PII 경로가 아니다
        raise NotImplementedError("기밀 리트리버는 score() 로 쓴다")

    async def score(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        """요청 하나(창 하나)의 쌍별 여유값과 문장 주의."""
        await self._ensure_process()
        try:
            return (await self._rpc(request))["pairs"]
        finally:
            self.residency.touch()


_process: RetrieverProcess | None = None


def get() -> RetrieverProcess:
    global _process
    if _process is None:
        _process = RetrieverProcess()
    return _process
