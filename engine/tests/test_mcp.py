"""
MCP 어댑터 검증 (PLAN §4, U2 Phase 1 완료 기준).

실제 엔진 프로세스를 uvicorn 으로 띄우고, MCP Python SDK 의 Streamable HTTP 클라이언트로
`/mcp` 에 붙어 tools/list 와 핵심 도구(scan_text/scan_file/get_status)를 실호출한다.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("mcp")

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, port: int):
        from app import config
        from app.main import app

        config.BOUND_PORT = port
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            if getattr(self.server, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError("uvicorn 기동 실패")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    srv = _Server(port)
    srv.start()
    yield f"http://127.0.0.1:{port}/mcp"
    srv.stop()


async def _drive(url: str) -> dict:
    out: dict = {}
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            out["tool_names"] = sorted(t.name for t in tools.tools)

            r = await session.call_tool("scan_text", {"text": "성명: 홍길동 / hong@example.com"})
            out["scan_text"] = r.structuredContent

            r = await session.call_tool("get_status", {})
            out["get_status"] = r.structuredContent
    return out


def test_mcp_tools(server, tmp_path):
    from app.core import model_status

    result = anyio.run(_drive, server)

    # tools/list — 6종(scan_file/scan_files, secure_search/list 쌍 포함) 전부 노출
    expected = {
        "scan_text", "scan_file", "scan_files", "mask_text",
        "secure_read_file", "secure_search_files", "secure_list_files", "get_status",
    }
    assert expected.issubset(set(result["tool_names"])), result["tool_names"]

    # scan_text — 룰베이스 폴백 제거 후, PII 모델이 로컬에 없으면 미검사 통과가
    # 정상이다(§PLAN 9.2). 있으면 실제 마스킹을 기대한다 — 단, 이름/이메일 중
    # 어느 쪽을 잡는지는 실행마다 갈렸다(모델 정확도/비결정성 이슈, MCP 응답은
    # originalText 를 안 주므로(PR #57) 특정 필드 대신 최소 1건 마스킹만 확인).
    st = result["scan_text"]
    if model_status.pii_ready():
        assert st["stats"]["piiCount"] >= 1
    else:
        assert st["scanStatus"] == "models_not_ready"
        assert "홍길동" in st["maskedText"]

    # get_status — 시그니처·detector·정책
    gs = result["get_status"]
    assert gs["service"] == "securedoc-gateway"
    assert "pii_encoder" in gs["detectors"]["pii"]
    assert gs["policy"]["injection"] in ("mask", "block")


def test_mcp_scan_file(server, tmp_path):
    from app.core import model_status

    f = tmp_path / "sample.txt"
    f.write_text("연락처 010-1234-5678 / 이메일 a@b.com", encoding="utf-8")

    async def _drive_file(url: str) -> dict:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r = await session.call_tool("scan_file", {"file_path": str(f)})
                return r.structuredContent

    res = anyio.run(_drive_file, server)
    assert res["path"].endswith("sample.txt")
    if model_status.pii_ready():
        assert res["scanStatus"] == "ok"
        assert res["stats"]["piiCount"] >= 1
    else:
        assert res["scanStatus"] == "models_not_ready"
