"""PII 외부 1000/100 청킹을 다시 도입하지 않도록 지키는 회귀 테스트."""

from __future__ import annotations

import asyncio

from app.core.pipeline import orchestrator


class _SpyPiiDetector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    async def detect(self, text: str, *, meta=None):
        self.calls.append((text, meta))
        return []


def test_pii_detector_receives_whole_document_once(monkeypatch):
    text = "가" * 2500
    detector = _SpyPiiDetector()
    monkeypatch.setattr(orchestrator.registry, "get_pii_detector", lambda: detector)
    monkeypatch.setattr(orchestrator.credentials, "detect", lambda _text: [])

    assert asyncio.run(orchestrator._detect_pii(text)) == []
    assert detector.calls == [
        (text, {"chunk_index": 0, "total_chunks": 1, "offset": 0})
    ]
