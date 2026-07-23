"""
app/adapters/mcp/tools.py
MCP 도구 6종 (PLAN §4 표) — 전부 U1 core 파이프라인/파서/디텍터/마스커를 그대로 호출.

핵심 원칙(PLAN §2): 코어는 순수 엔진, MCP 는 어댑터 한 겹. 로직 중복 구현 금지 —
모든 탐지/마스킹은 core.pipeline.run_pipeline / core.masker.apply_masking 에 위임한다.

도구 목록(PLAN §4):
  scan_text                         텍스트 PII/인젝션 탐지 + 마스킹본
  scan_file / scan_files            파일/폴더 검사(파서 경유)
  mask_text                         탐지 목록으로 마스킹만 재적용
  secure_read_file                  정책 적용 파일 읽기(항상 파이프라인 통과, §4.2 게이트)
  secure_search_files / secure_list_files   정책 적용 검색/목록
  get_status                        엔진 상태·정책·활성 detector
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app import config
from app.core.detectors import registry
from app.core.masker import masker
from app.core.pipeline.orchestrator import run_pipeline
from app.store import db

# stateless_http: FastAPI 마운트 시 세션 상태를 요청 간 유지하지 않아 마운트가 단순·견고.
# 각 요청이 독립적이므로 REST 와 프로세스 하나를 공유해도 상태 얽힘이 없다(PLAN §4).
mcp = FastMCP("securedoc-gateway", stateless_http=True)

# 검색/목록에서 텍스트로 취급할 확장자 (기존 securedoc_mcp 계승)
_TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
    ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".go", ".rs", ".sh", ".ps1", ".bat", ".toml", ".ini",
    ".cfg", ".conf", ".log", ".sql", ".env", ".example",
}
_DOCUMENT_EXTS = set(config.SUPPORTED_EXTENSIONS) | set(config.UNSUPPORTED_EXTENSIONS)

_PROJECT_ROOT = Path(os.environ.get("SECUREDOC_PROJECT_ROOT", os.getcwd())).resolve()


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────────
def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p.resolve()


def _file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _DOCUMENT_EXTS:
        return "document"
    if suffix in _TEXT_EXTS or path.name.lower().startswith(".env"):
        return "text"
    return "binary"


def _public(result: dict[str, Any]) -> dict[str, Any]:
    """MCP 응답용 뷰. 파이프라인 결과에 요약 플래그를 덧붙인다.

    originalText 는 세션 중 HITL/diff 참고용으로만 포함(store 미저장은 db 책임, PLAN §9.1).
    """
    pii = result.get("piiItems", [])
    inj = result.get("injectionItems", [])
    detection_count = len(pii) + len(inj)
    return {
        "originalText": result.get("originalText", ""),
        "maskedText": result.get("maskedText", ""),
        "piiItems": pii,
        "injectionItems": inj,
        "stats": result.get("stats", {}),
        "scanStatus": result.get("scanStatus", "ok"),
        "reason": result.get("reason"),
        "blocked": result.get("blocked", False),
        "hasPii": len(pii) > 0,
        "hasInjection": len(inj) > 0,
        "detectionCount": detection_count,
        "recommendedAction": "mask_before_upload" if detection_count else "allow",
        "policy": result.get("policy", {"injection": config.INJECTION_POLICY}),
    }


async def _scan_bytes(file_bytes: bytes, mime_type: str, file_name: str) -> dict[str, Any]:
    result = await run_pipeline(
        file_bytes=file_bytes, mime_type=mime_type, file_name=file_name, wrap_file=False
    )
    _record(file_name, result)
    return result


def _record(file_name: str, result: dict[str, Any]) -> None:
    """탐지 통계를 store 에 남긴다(source=mcp). 원문은 저장하지 않음(PLAN §9.1)."""
    import uuid

    try:
        db.record_job(str(uuid.uuid4()), file_name=file_name, source="mcp", result=result)
    except Exception:  # noqa: BLE001 - 통계 기록 실패가 도구 응답을 막지 않게
        pass


# ── 도구 1: scan_text ─────────────────────────────────────────────────────────
@mcp.tool()
async def scan_text(text: str) -> dict[str, Any]:
    """텍스트에서 개인정보(PII)와 프롬프트 인젝션을 탐지하고 마스킹본을 반환한다.

    사람 확인(HITL) 없이 즉시 탐지 결과를 돌려준다. 신뢰할 수 없는 출처의 텍스트나
    민감정보가 포함될 수 있는 텍스트를 외부 LLM/이메일/다른 문서로 넘기기 전에 먼저 검사하라.

    반환: originalText, maskedText, piiItems/injectionItems(각 {type,start,end,text,
    confidence,source}, 좌표는 원문 기준 0-based), stats, hasPii/hasInjection, policy.
    """
    result = await run_pipeline(text=text, file_name="prompt.txt", wrap_file=False)
    _record("prompt.txt", result)
    return _public(result)


# ── 도구 2: scan_file ─────────────────────────────────────────────────────────
@mcp.tool()
async def scan_file(file_path: str, mime_type: str = "") -> dict[str, Any]:
    """로컬 파일을 파서 경유로 검사한다(PII/인젝션 탐지 + 마스킹본).

    TXT/PDF/DOCX 를 지원한다(스캔 PDF 및 HWP/HWPX/XLSX/PPTX 는 v1 미지원 → scanStatus 로
    표시 후 통과, PLAN §9.2). mime_type 을 비우면 확장자로 추정한다. 반환 형식은 scan_text 와
    동일하며 path 필드가 추가된다.
    """
    path = _resolve(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

    if not mime_type:
        guessed, _ = mimetypes.guess_type(str(path))
        mime_type = guessed or "application/octet-stream"

    file_bytes = path.read_bytes()
    result = await _scan_bytes(file_bytes, mime_type, path.name)
    out = _public(result)
    out["path"] = str(path)
    return out


# ── 도구 3: scan_files ────────────────────────────────────────────────────────
@mcp.tool()
async def scan_files(root: str = ".", pattern: str = "*", max_results: int = 20) -> dict[str, Any]:
    """폴더 내 여러 파일을 일괄 검사한다(업로드/공유 전 배치 점검용).

    텍스트/문서 파일만 검사하고 미지원 바이너리는 건너뛴다. 각 항목은 scan_file 요약
    (탐지 카운트·maskedText·항목)을 담는다.
    """
    root_path = _resolve(root)
    if not root_path.is_dir():
        raise NotADirectoryError(f"디렉터리가 아닙니다: {root_path}")

    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in sorted(root_path.rglob(pattern)):
        if len(items) >= max_results:
            break
        if not path.is_file():
            continue
        if _file_kind(path) == "binary":
            skipped.append({"path": str(path), "reason": "unsupported-binary"})
            continue
        try:
            scanned = await scan_file(str(path))
            items.append(
                {
                    "path": str(path),
                    "detectionCount": scanned["detectionCount"],
                    "piiCount": scanned["stats"].get("piiCount", 0),
                    "injectionCount": scanned["stats"].get("injectionCount", 0),
                    "hasPii": scanned["hasPii"],
                    "hasInjection": scanned["hasInjection"],
                    "scanStatus": scanned["scanStatus"],
                    "recommendedAction": scanned["recommendedAction"],
                    "maskedText": scanned["maskedText"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - 사이트별 독립 에러 처리(PLAN §11)
            skipped.append({"path": str(path), "reason": str(exc)})

    total = sum(it["detectionCount"] for it in items)
    return {
        "root": str(root_path),
        "pattern": pattern,
        "fileCount": len(items),
        "skippedCount": len(skipped),
        "detectionCount": total,
        "recommendedAction": "mask_before_upload" if total else "allow",
        "items": items,
        "skipped": skipped,
    }


# ── 도구 4: mask_text ─────────────────────────────────────────────────────────
@mcp.tool()
async def mask_text(
    text: str,
    pii_items: list[dict[str, Any]],
    injection_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """탐지 항목 목록으로 텍스트에 마스킹만 재적용한다(재탐지 없음).

    scan_* 가 이미 maskedText 를 함께 주므로 보통은 불필요하다. 항목 일부를 편집(예: 특정
    PII만 제외)한 뒤 마스킹 결과를 다시 만들고 싶을 때 사용한다. 반환: {maskedText, applied}.
    """
    items = list(pii_items) + list(injection_items or [])
    out = masker.apply_masking(text, items)
    return {"maskedText": out["masked_text"], "applied": out["applied"]}


# ── 도구 5: secure_read_file (§4.2 게이트) ────────────────────────────────────
@mcp.tool()
async def secure_read_file(file_path: str) -> dict[str, Any]:
    """정책을 적용해 파일을 읽는다 — 항상 파이프라인을 통과한 마스킹본만 반환(PLAN §4.2).

    클라이언트의 기본 Read 도구를 이 도구로 대체하면, 원본이 파이프라인을 우회해
    새어나가는 경로를 없앨 수 있다. 문서/텍스트 파일은 검사 후 maskedText 를 반환하고,
    바이너리는 원본을 반환하지 않는다. decision: masked | clean | blocked.
    """
    path = _resolve(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

    kind = _file_kind(path)
    if kind == "binary":
        return {
            "path": str(path),
            "decision": "blocked",
            "reason": "바이너리 파일은 보안 게이트에서 원본을 반환하지 않습니다.",
            "content": "",
            "policy": {"injection": config.INJECTION_POLICY},
        }

    if not mimetypes.guess_type(str(path))[0] and kind == "text":
        mime = "text/plain"
    else:
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

    result = await _scan_bytes(path.read_bytes(), mime, path.name)
    pub = _public(result)
    decision = "masked" if (pub["hasPii"] or pub["hasInjection"]) else "clean"
    return {
        "path": str(path),
        "decision": decision,
        "content": pub["maskedText"],
        "scanStatus": pub["scanStatus"],
        "reason": pub["reason"],
        "stats": pub["stats"],
        "piiItems": pub["piiItems"],
        "injectionItems": pub["injectionItems"],
        "policy": pub["policy"],
    }


# ── 도구 6a: secure_list_files ────────────────────────────────────────────────
@mcp.tool()
async def secure_list_files(root: str = ".", pattern: str = "*", max_results: int = 200) -> dict[str, Any]:
    """정책 적용 파일 목록을 반환한다(파일 내용은 반환하지 않음)."""
    root_path = _resolve(root)
    if not root_path.is_dir():
        raise NotADirectoryError(f"디렉터리가 아닙니다: {root_path}")

    files: list[dict[str, Any]] = []
    for path in sorted(root_path.rglob(pattern)):
        if len(files) >= max_results:
            break
        if path.is_file():
            files.append({"path": str(path), "name": path.name, "kind": _file_kind(path)})
    return {"root": str(root_path), "count": len(files), "files": files}


# ── 도구 6b: secure_search_files ──────────────────────────────────────────────
@mcp.tool()
async def secure_search_files(
    root: str, query: str, pattern: str = "*", max_results: int = 50
) -> dict[str, Any]:
    """정책을 적용해 텍스트 파일을 검색한다 — 매칭 라인은 PII/인젝션 마스킹 후 반환한다.

    검색 결과 스니펫도 원본이 새어나가지 않도록 core 마스커를 통과시킨다(PLAN §4.2).
    """
    root_path = _resolve(root)
    if not root_path.is_dir():
        raise NotADirectoryError(f"디렉터리가 아닙니다: {root_path}")

    results: list[dict[str, Any]] = []
    for path in sorted(root_path.rglob(pattern)):
        if len(results) >= max_results:
            break
        if not path.is_file() or _file_kind(path) != "text":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            try:
                text = path.read_text(encoding="cp949")
            except (UnicodeDecodeError, OSError):
                continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if query in line:
                # 라인 스니펫만 마스킹(통계 job 기록 없이 core 파이프라인 통과)
                line_res = await run_pipeline(text=line, file_name="search.txt", wrap_file=False)
                results.append(
                    {"path": str(path), "line": line_no, "lineText": line_res["maskedText"]}
                )
                if len(results) >= max_results:
                    break
    return {"root": str(root_path), "query": query, "count": len(results), "results": results}


# ── 도구 7: get_status ────────────────────────────────────────────────────────
@mcp.tool()
async def get_status() -> dict[str, Any]:
    """엔진 상태·정책·활성 detector 를 반환한다.

    service 시그니처, 바인딩 포트, 활성 PII/인젝션 detector, 인젝션 정책(mask|block),
    지원 확장자, 누적 탐지 통계, GPU 상주 정책 상태(PLAN §4.1)를 담는다.
    """
    try:
        stats = db.stats_summary()
    except Exception:  # noqa: BLE001
        stats = {}
    return {
        "service": config.SERVICE_NAME,
        "status": "ok",
        "version": "0.1.0",
        "port": config.BOUND_PORT,
        "transport": "streamable-http",
        "detectors": registry.active_detectors(),
        "policy": {"injection": config.INJECTION_POLICY},
        "supportedExtensions": sorted(config.SUPPORTED_EXTENSIONS),
        "unsupportedExtensions": sorted(config.UNSUPPORTED_EXTENSIONS),
        "stats": stats,
        "gpuResidency": registry.residency_status(),  # PLAN §4.1: PII 항시상주 / 인젝션 유휴언로드
    }
