"""긴 문서(50장)에서 파이프라인이 자원을 무제한으로 쓰지 않는지 지킨다.

배경(실사용자 리포트): "50장짜리 문서를 넣으면 GPU 가 99% 돌다가 터져서 결과가
안 나온다".

두 가지가 겹쳐 있었다.
  1) _detect_all 이 청크를 상한 없이 asyncio.gather 로 전부 띄웠다. 청크가 2~3개인
     문서에서 Solar API 대기를 겹치게 하려던 최적화인데, 문서가 길면 팬아웃이 그대로
     커진다 — 실측: 50장(10만 자)이면 111개, 15만 자면 167개가 동시에 진입했다.
  2) 검출기 서브프로세스의 응답 대기(readline)에 상한이 없었다. 그 대기는
     _request_lock 안에서 일어나므로, 프로세스가 한 번 멎으면 뒤에 줄 선 청크가
     전부 같이 멎어 영영 결과가 안 나온다.
"""

from __future__ import annotations

import asyncio

import pytest

from app import config
from app.core.pipeline.orchestrator import _detect_all, _split_chunks


class _SpyDetector:
    """추론 대신 동시 진입 수만 센다."""

    def __init__(self) -> None:
        self.inflight = 0
        self.peak = 0
        self.calls = 0

    async def detect(self, text, *, meta=None):
        self.inflight += 1
        self.calls += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0)  # 다른 태스크에 양보 — 겹칠 기회를 준다
            return []
        finally:
            self.inflight -= 1


def _pages(n: int, per_page: int = 2000) -> str:
    return "가" * (n * per_page)


def test_긴_문서도_동시_진입이_상한을_넘지_않는다():
    asyncio.run(_긴_문서())


async def _긴_문서():
    text = _pages(50)
    chunks = _split_chunks(text, config.CHUNK_SIZE)
    # 전제가 깨지면(청크 분할이 바뀌면) 이 테스트의 의미가 없어지므로 같이 못 박는다.
    assert len(chunks) > 100, f"50장이 청크 {len(chunks)}개 — 팬아웃 검증의 전제가 깨졌다"

    spy = _SpyDetector()
    await _detect_all(spy, text, chunks)

    assert spy.calls == len(chunks), "청크가 누락됐다 — 상한을 두되 빠뜨리면 안 된다"
    assert spy.peak <= config.DETECT_CONCURRENCY, (
        f"동시 진입 {spy.peak} 이 상한 {config.DETECT_CONCURRENCY} 을 넘었다"
    )


def test_짧은_문서는_여전히_겹쳐서_처리된다():
    asyncio.run(_짧은_문서())


async def _짧은_문서():
    """상한을 두느라 작은 문서의 지연 이득까지 없애면 안 된다.

    기본 상한(8)은 웬만한 짧은 문서의 청크 수보다 크므로, 청크가 그보다 적으면
    예전처럼 전부 동시에 진입해야 한다."""
    text = _pages(1)
    chunks = _split_chunks(text, config.CHUNK_SIZE)
    assert 1 < len(chunks) <= config.DETECT_CONCURRENCY

    spy = _SpyDetector()
    await _detect_all(spy, text, chunks)

    assert spy.peak == len(chunks), (
        f"짧은 문서인데 동시 진입이 {spy.peak} 뿐 — 상한이 과하게 좁다"
    )


class _HungStdout:
    """영원히 아무것도 안 주는 stdout — 멎은 서브프로세스."""

    async def readline(self):
        await asyncio.Event().wait()


class _HungStdin:
    def write(self, _data): ...
    async def drain(self): ...


class _HungProc:
    """응답하지 않는 서브프로세스."""

    def __init__(self) -> None:
        self.returncode = None
        self.stdin = _HungStdin()
        self.stdout = _HungStdout()
        self.stderr = None
        self.killed = False

    def terminate(self) -> None:
        self.returncode = -1

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.mark.parametrize("which", ["injection", "pii"])
def test_서브프로세스가_멎으면_영원히_기다리지_않는다(monkeypatch, which):
    asyncio.run(_멎은_프로세스(monkeypatch, which))


async def _멎은_프로세스(monkeypatch, which):
    monkeypatch.setattr(config, "DETECT_INFER_TIMEOUT_SEC", 0.3)

    if which == "injection":
        from app.core.detectors.injection.llm_mcp import InjectionLlmMcpDetector as Cls

        async def call(d):
            return await d._infer("텍스트")
    else:
        from app.core.detectors.pii.encoder import EncoderPiiDetector as Cls

        async def call(d):
            return await d._infer_batch(["텍스트"])

    # __init__ 은 모델 경로/프로세스를 요구하므로 건너뛰고 필요한 속성만 심는다.
    detector = object.__new__(Cls)
    detector._process = _HungProc()
    detector._request_lock = asyncio.Lock()
    detector._next_id = 1

    # 상한이 없으면 여기서 영원히 멎는다 — wait_for 가 그걸 실패로 잡아준다.
    with pytest.raises(RuntimeError, match="응답하지 않음"):
        await asyncio.wait_for(call(detector), timeout=10)
