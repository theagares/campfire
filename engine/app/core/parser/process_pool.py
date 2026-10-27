"""Bounded, restartable process isolation for document parsing.

``asyncio.wait_for(asyncio.to_thread(...))`` can stop waiting, but it cannot
stop the parser thread. A malformed document can therefore keep consuming CPU
and memory after its request returned. This module owns a small set of parser
processes and their complete lifecycle:

* no more than ``worker_count`` documents parse at once;
* only ``max_queue_size`` additional requests may wait;
* a timed-out or broken worker is terminated and lazily replaced;
* parser and process I/O never block the async event loop.

The worker returns embedded images as well as text so the existing parser to
image-OCR contract remains unchanged across the process boundary.
"""

from __future__ import annotations

import asyncio
import atexit
import importlib
import multiprocessing
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.connection import Connection
from typing import Callable

from app import config

ParseResult = tuple[str, str, str | None]
ParseResponse = tuple[ParseResult, list[bytes] | None]


class ParserPoolBusy(RuntimeError):
    """All parser workers and bounded queue slots are occupied."""


class ParserWorkerError(RuntimeError):
    """A worker failed outside the parser's normal result contract."""


class ParserTimedOut(TimeoutError):
    """The worker exceeded its deadline and was terminated."""

    def __init__(self, timeout: float, worker_pid: int | None = None) -> None:
        self.timeout = timeout
        self.worker_pid = worker_pid
        super().__init__(f"parser exceeded {timeout:g}s (worker pid={worker_pid})")


def _parser_worker_main(
    connection: Connection,
    parser_module: str,
    parser_name: str,
) -> None:
    """Serve parse calls in one isolated process."""
    # A dedicated process group lets a timeout also stop child converters such
    # as LibreOffice. Windows uses taskkill /T in the parent for the same scope.
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            pass

    try:
        parser: Callable[..., ParseResult] = getattr(
            importlib.import_module(parser_module), parser_name
        )
        connection.send(("ready", os.getpid()))

        while True:
            try:
                message = connection.recv()
            except EOFError:
                break
            if message is None:
                break

            request_id, file_bytes, mime_type, file_name, collect_images = message
            images: list[bytes] | None = [] if collect_images else None
            try:
                result = parser(file_bytes, mime_type, file_name, images)
                connection.send(("result", request_id, result, images))
            except BaseException as exc:  # noqa: BLE001 - isolate broken parsers
                remote_traceback = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )[-8000:]
                connection.send(
                    ("error", request_id, type(exc).__name__, str(exc), remote_traceback)
                )
    except BaseException as exc:  # noqa: BLE001 - report startup/import failure
        try:
            connection.send(("startup_error", type(exc).__name__, str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class _ParserWorker:
    def __init__(
        self,
        context: multiprocessing.context.BaseContext,
        parser_module: str,
        parser_name: str,
        start_timeout: float,
    ) -> None:
        self._context = context
        self._parser_module = parser_module
        self._parser_name = parser_name
        self._start_timeout = start_timeout
        self._process: multiprocessing.Process | None = None
        self._connection: Connection | None = None
        self._state_lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        with self._state_lock:
            process = self._process
            return process.pid if process is not None and process.is_alive() else None

    def parse(
        self,
        file_bytes: bytes,
        mime_type: str,
        file_name: str,
        collect_images: bool,
        timeout: float,
    ) -> ParseResponse:
        process, connection = self._ensure_started()
        request_id = uuid.uuid4().hex
        started = time.monotonic()

        try:
            connection.send(
                (request_id, file_bytes, mime_type, file_name, collect_images)
            )
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0 or not connection.poll(remaining):
                timed_out_pid = process.pid
                self._discard(process, connection)
                raise ParserTimedOut(timeout, timed_out_pid)
            message = connection.recv()
        except ParserTimedOut:
            raise
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._discard(process, connection)
            raise ParserWorkerError(f"parser worker connection failed: {exc}") from exc

        kind = message[0] if isinstance(message, tuple) and message else None
        if kind == "result" and len(message) == 4 and message[1] == request_id:
            result, images = message[2], message[3]
            if (
                not isinstance(result, tuple)
                or len(result) != 3
                or not isinstance(result[0], str)
                or not isinstance(result[1], str)
            ):
                self._discard(process, connection)
                raise ParserWorkerError("parser worker returned an invalid result")
            return result, images

        self._discard(process, connection)
        if kind == "error" and len(message) >= 5 and message[1] == request_id:
            raise ParserWorkerError(
                f"parser worker raised {message[2]}: {message[3]}\n{message[4]}"
            )
        raise ParserWorkerError(f"unexpected parser worker response: {kind!r}")

    def _ensure_started(self) -> tuple[multiprocessing.Process, Connection]:
        with self._state_lock:
            current_process = self._process
            current_connection = self._connection
            if (
                current_process is not None
                and current_connection is not None
                and current_process.is_alive()
            ):
                return current_process, current_connection

            self._clear_state_locked()
            parent_connection, child_connection = self._context.Pipe(duplex=True)
            process = self._context.Process(
                target=_parser_worker_main,
                args=(child_connection, self._parser_module, self._parser_name),
                name="campfire-parser",
                daemon=True,
            )
            process.start()
            child_connection.close()
            self._process = process
            self._connection = parent_connection

        try:
            if not parent_connection.poll(self._start_timeout):
                self._discard(process, parent_connection)
                raise ParserWorkerError(
                    f"parser worker did not start within {self._start_timeout:g}s"
                )
            ready = parent_connection.recv()
        except (EOFError, OSError) as exc:
            self._discard(process, parent_connection)
            raise ParserWorkerError(f"parser worker failed to start: {exc}") from exc

        if not (isinstance(ready, tuple) and ready and ready[0] == "ready"):
            self._discard(process, parent_connection)
            if isinstance(ready, tuple) and ready and ready[0] == "startup_error":
                raise ParserWorkerError(
                    f"parser worker startup failed ({ready[1]}): {ready[2]}"
                )
            raise ParserWorkerError(f"unexpected parser worker startup response: {ready!r}")
        return process, parent_connection

    def close(self) -> None:
        with self._state_lock:
            process = self._process
            connection = self._connection
            self._process = None
            self._connection = None

        if connection is not None:
            try:
                connection.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
            finally:
                connection.close()
        if process is not None:
            process.join(timeout=0.5)
            if process.is_alive():
                self._terminate_process_tree(process)

    def abort(self) -> None:
        """Force the current worker down, including during app shutdown."""
        with self._state_lock:
            process = self._process
            connection = self._connection
            self._process = None
            self._connection = None
        if connection is not None:
            connection.close()
        if process is not None and process.is_alive():
            self._terminate_process_tree(process)

    def _discard(
        self,
        process: multiprocessing.Process,
        connection: Connection,
    ) -> None:
        with self._state_lock:
            owns_process = self._process is process
            owns_connection = self._connection is connection
            if self._process is process:
                self._process = None
            if self._connection is connection:
                self._connection = None
        # close() may have concurrently detached and terminated this worker.
        # Only the thread that detached live state owns process-tree cleanup.
        if owns_connection:
            connection.close()
        if owns_process:
            if process.is_alive():
                self._terminate_process_tree(process)
            else:
                process.join(timeout=0)

    def _clear_state_locked(self) -> None:
        old_connection = self._connection
        old_process = self._process
        self._connection = None
        self._process = None
        if old_connection is not None:
            old_connection.close()
        if old_process is not None:
            if old_process.is_alive():
                self._terminate_process_tree(old_process)
            else:
                old_process.join(timeout=0)

    @staticmethod
    def _terminate_process_tree(process: multiprocessing.Process) -> None:
        pid = process.pid
        if pid is None:
            return

        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                process.terminate()
        else:
            try:
                # Never signal our own process group if setsid() failed.
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGTERM)
                else:
                    process.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                process.terminate()

        process.join(timeout=0.5)
        if process.is_alive():
            if sys.platform != "win32":
                try:
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            if process.is_alive():
                process.kill()
            process.join(timeout=0.5)


class ParserProcessPool:
    """A bounded set of persistent, independently restartable parser workers."""

    def __init__(
        self,
        worker_count: int,
        max_queue_size: int,
        *,
        parser_module: str = "app.core.parser",
        parser_name: str = "parse_document",
        worker_start_timeout: float = 10.0,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        if max_queue_size < 0:
            raise ValueError("max_queue_size cannot be negative")

        context = multiprocessing.get_context("spawn")
        self._workers = [
            _ParserWorker(context, parser_module, parser_name, worker_start_timeout)
            for _ in range(worker_count)
        ]
        self._available: queue.Queue[_ParserWorker] = queue.Queue()
        for worker in self._workers:
            self._available.put(worker)
        self._capacity = threading.BoundedSemaphore(worker_count + max_queue_size)
        self._executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="campfire-parser-control",
        )
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._close_lock:
            return self._closed

    @property
    def worker_pids(self) -> tuple[int, ...]:
        return tuple(pid for worker in self._workers if (pid := worker.pid) is not None)

    async def parse(
        self,
        file_bytes: bytes,
        mime_type: str,
        file_name: str,
        *,
        collect_images: bool,
        timeout: float,
    ) -> ParseResponse:
        with self._close_lock:
            if self._closed:
                raise ParserWorkerError("parser pool is closed")
            if not self._capacity.acquire(blocking=False):
                raise ParserPoolBusy("parser queue is full")
            try:
                future = asyncio.get_running_loop().run_in_executor(
                    self._executor,
                    self._parse_and_release_slot,
                    file_bytes,
                    mime_type,
                    file_name,
                    collect_images,
                    timeout,
                )
            except BaseException:
                self._capacity.release()
                raise

        # Shield keeps a cancelled HTTP request from cancelling a queued
        # controller future before it can release the capacity slot.
        return await asyncio.shield(future)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        # Break active pipe waits first; controller threads then unwind quickly.
        for worker in self._workers:
            worker.abort()
        self._executor.shutdown(wait=True, cancel_futures=True)
        for worker in self._workers:
            worker.close()

    def _parse_and_release_slot(
        self,
        file_bytes: bytes,
        mime_type: str,
        file_name: str,
        collect_images: bool,
        timeout: float,
    ) -> ParseResponse:
        try:
            worker = self._available.get_nowait()
            try:
                return worker.parse(
                    file_bytes, mime_type, file_name, collect_images, timeout
                )
            finally:
                self._available.put(worker)
        finally:
            self._capacity.release()


_default_pool: ParserProcessPool | None = None
_default_pool_lock = threading.Lock()


def get_default_parse_pool() -> ParserProcessPool:
    global _default_pool
    with _default_pool_lock:
        if _default_pool is None or _default_pool.closed:
            _default_pool = ParserProcessPool(
                config.PARSE_WORKERS,
                config.PARSE_QUEUE_SIZE,
                worker_start_timeout=config.PARSE_WORKER_START_TIMEOUT_SEC,
            )
        return _default_pool


def shutdown_default_parse_pool() -> None:
    global _default_pool
    with _default_pool_lock:
        pool = _default_pool
        _default_pool = None
    if pool is not None:
        pool.close()


atexit.register(shutdown_default_parse_pool)
