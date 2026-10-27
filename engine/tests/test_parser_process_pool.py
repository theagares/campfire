"""The parser deadline must end work, not only stop awaiting it."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.core.parser.process_pool import (
    ParserPoolBusy,
    ParserProcessPool,
    ParserTimedOut,
)


def _pool(*, queue_size: int = 1) -> ParserProcessPool:
    return ParserProcessPool(
        1,
        queue_size,
        parser_module="tests.parser_pool_fixture",
        parser_name="parse_for_test",
        worker_start_timeout=10,
    )


def test_timeout_kills_worker_and_next_document_recovers():
    """Repeated hangs leave no old worker and do not delay a healthy parse."""
    baseline_threads = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("campfire-parser-control")
    }
    pool = _pool()

    async def scenario():
        timed_out_pids: list[int] = []
        for _ in range(3):
            started = time.monotonic()
            with pytest.raises(ParserTimedOut) as caught:
                await pool.parse(
                    b"2|hung", "text/plain", "hung.txt",
                    collect_images=False, timeout=0.1,
                )
            assert time.monotonic() - started < 1.5
            assert caught.value.worker_pid is not None
            timed_out_pids.append(caught.value.worker_pid)
            assert not pool.worker_pids

        started = time.monotonic()
        result, images = await pool.parse(
            b"0|healthy", "text/plain", "healthy.txt",
            collect_images=True, timeout=2,
        )
        assert time.monotonic() - started < 1.5
        assert result == ("healthy", "ok", None)
        assert images == [b"fixture-image"]
        assert pool.worker_pids
        assert pool.worker_pids[0] not in timed_out_pids

    try:
        asyncio.run(scenario())
    finally:
        pool.close()
    assert not pool.worker_pids
    remaining_threads = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("campfire-parser-control")
    }
    assert remaining_threads <= baseline_threads


def test_queue_is_bounded_while_worker_is_busy():
    pool = _pool(queue_size=0)

    async def scenario():
        running = asyncio.create_task(
            pool.parse(
                b"0.5|first", "text/plain", "first.txt",
                collect_images=False, timeout=2,
            )
        )
        await asyncio.sleep(0.1)
        with pytest.raises(ParserPoolBusy):
            await pool.parse(
                b"0|second", "text/plain", "second.txt",
                collect_images=False, timeout=2,
            )
        assert (await running)[0] == ("first", "ok", None)

    try:
        asyncio.run(scenario())
    finally:
        pool.close()
