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
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app import config
from app.core import activity as activity_bus
from app.core.detectors import registry
from app.core.masker import masker
from app.core.pipeline.orchestrator import run_pipeline
from app.store import db

# stateless_http: FastAPI 마운트 시 세션 상태를 요청 간 유지하지 않아 마운트가 단순·견고.
# 각 요청이 독립적이므로 REST 와 프로세스 하나를 공유해도 상태 얽힘이 없다(PLAN §4).
mcp = FastMCP("campfire", stateless_http=True)

# 검색/목록에서 텍스트로 취급할 확장자 (기존 securedoc_mcp 계승)
_TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
    ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".go", ".rs", ".sh", ".ps1", ".bat", ".toml", ".ini",
    ".cfg", ".conf", ".log", ".sql",
}
_DOCUMENT_EXTS = set(config.SUPPORTED_EXTENSIONS) | set(config.UNSUPPORTED_EXTENSIONS)

# read_text() 로 그대로 읽을 수 있는 확장자 = secure_search_files 가 훑을 수 있는 대상.
#
# _TEXT_EXTS 와 따로 두는 이유: ".txt" 는 파서가 다루는 포맷이라 config.SUPPORTED_EXTENSIONS
# 에도 들어 있고, _file_kind() 는 문서 판정을 먼저 하므로 ".txt" 의 kind 가 "document" 다.
# 그래서 kind == "text" 만 보던 secure_search_files 에서 **가장 흔한 텍스트 파일이 통째로
# 빠져 있었다**(도구 설명은 "텍스트 파일을 검색한다" 인데 .txt 는 한 건도 안 나왔다).
# 분류를 바꾸면 secure_read_file 의 mime 추정 등 다른 판단까지 흔들리므로, "검색 가능한
# 평문" 이라는 별도 기준을 여기 둔다.
_SEARCHABLE_TEXT_EXTS = _TEXT_EXTS | {".txt"}

# 자격증명이 담기는 파일. 예전엔 .env 가 위 _TEXT_EXTS 에 들어 있어서 "text" 로 분류됐고,
# 그러면 secure_read_file 이 내용을 읽어 마스킹본을 돌려줬다 — 그런데 마스커가 지우는 건
# PII 와 인젝션뿐이라 UPSTAGE_API_KEY=..., AWS 키, DB 비밀번호는 그대로 통과했다.
# 바이너리는 막으면서 키 파일은 열어주는, 방향이 반대인 구멍이었다.
#
# 이런 파일은 "마스킹해서 주는" 대상이 아니라 애초에 게이트를 통과시키지 않는다 —
# 무엇이 비밀인지 내용만 보고 알아낼 방법이 없기 때문이다.
_SECRET_FILE_NAMES = {
    ".netrc", "_netrc", ".pgpass", ".htpasswd", ".npmrc", ".pypirc",
    "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_SECRET_SUFFIXES = {
    ".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".ppk", ".kdbx", ".asc", ".gpg",
}

_PROJECT_ROOT = Path(os.environ.get("SECUREDOC_PROJECT_ROOT", os.getcwd())).resolve()


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────────
def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p.resolve()


def _is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    # .env / .env.local / .env.production ... 전부 포함. .env.example 처럼 보통은
    # 값이 비어 있는 것도 같이 걸리는데, 실키가 들어 있는 걸 여러 번 봤으므로
    # 구분하지 않는다(내용을 보고 판단할 수 없다는 게 이 목록의 전제다).
    return (
        name.startswith(".env")
        or name in _SECRET_FILE_NAMES
        or path.suffix.lower() in _SECRET_SUFFIXES
    )


def _file_kind(path: Path) -> str:
    if _is_secret_file(path):
        return "secret"
    suffix = path.suffix.lower()
    if suffix in _DOCUMENT_EXTS:
        return "document"
    if suffix in _TEXT_EXTS:
        return "text"
    return "binary"


# 내용을 어떤 형태로도 돌려주지 않는 종류와, 그 사유 문구.
_WITHHELD_KINDS = {
    "binary": "바이너리 파일은 보안 게이트에서 원본을 반환하지 않습니다.",
    "secret": (
        "자격증명 파일로 판단되어 내용을 반환하지 않습니다 — 마스킹은 개인정보와 인젝션만"
        " 지우므로 API 키·비밀번호는 걸러지지 않습니다."
    ),
}


def _redact_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """탐지 항목에서 원문 값(text)을 뺀 사본. 위치·종류·신뢰도만 남긴다.

    항목의 text 에는 탐지된 원문이 그대로 들어 있다(PII 는 주민번호·카드번호 실값,
    인젝션은 공격 문구 자체). MCP 소비자는 사람이 아니라 AI 이므로 이걸 그대로
    넘기면 두 가지가 동시에 깨진다:
      - 마스킹의 의미가 사라진다. 개인정보를 가리려고 검사했는데 실값을 같이 준다.
      - 인젝션 문구를 AI 에게 그대로 먹인다 — 막으려던 공격을 우리가 전달하는 꼴이다.
    """
    return [{k: v for k, v in item.items() if k != "text"} for item in items]


def _public(result: dict[str, Any]) -> dict[str, Any]:
    """MCP 응답용 뷰. 파이프라인 결과에 요약 플래그를 덧붙인다.

    **원문은 절대 넣지 않는다.** 원문(originalText)과 항목별 원문 값(item.text)은
    HTTP API 경로에서는 필요하다 — 그쪽 소비자는 확장의 검토 패널이고, 사람이 원문과
    마스킹본을 나란히 보고 항목별로 켜고 끄기 때문이다. 하지만 MCP 는 소비자가 AI
    자신이라 정반대다. 여기로 원문을 흘리면 "AI 에게 원문을 안 보여주려고" 검사한
    의미가 통째로 사라진다(실사용자 확인: scan_file 응답만으로 주민번호·카드번호·
    인젝션 문구가 모두 노출됐다).
    """
    pii = result.get("piiItems", [])
    inj = result.get("injectionItems", [])
    detection_count = len(pii) + len(inj)
    blocked = bool(result.get("blocked", False))
    if blocked:
        # 정책이 block 이면 "마스킹해서 쓰라"가 아니라 "쓰지 마라"다. 예전엔 blocked 를
        # 필드로만 실어 보내고 recommendedAction 은 그대로 mask_before_upload 였는데,
        # 이 응답을 읽는 건 사람이 아니라 AI 라 권고 문구가 실질적인 지시로 작동한다.
        recommended = "block"
    elif detection_count:
        recommended = "mask_before_upload"
    else:
        recommended = "allow"
    return {
        "maskedText": result.get("maskedText", ""),
        "piiItems": _redact_items(pii),
        "injectionItems": _redact_items(inj),
        "stats": result.get("stats", {}),
        "scanStatus": result.get("scanStatus", "ok"),
        "reason": result.get("reason"),
        "blocked": blocked,
        "hasPii": len(pii) > 0,
        "hasInjection": len(inj) > 0,
        "detectionCount": detection_count,
        "recommendedAction": recommended,
        "policy": result.get("policy", {"injection": config.INJECTION_POLICY}),
    }


async def _run_pipeline_broadcast(**kwargs: Any) -> dict[str, Any]:
    """run_pipeline 을 돌리되 진행 단계를 대시보드 "처리현황" 으로 방송한다.

    HTTP(확장) 경로는 jobs.py 가 job_started → emit → job_finished 로 이걸 하고 있는데
    MCP 경로만 빠져 있었다. 그래서 AI 클라이언트로 검사를 돌리면 앱 처리현황이 계속
    idle 이었다 — 단계가 눈에 안 띄게 빨리 지나가서가 아니라, 이벤트가 하나도 나가지
    않아서다(어댑터 전체에 activity 호출이 없었다).

    job_registry 에는 기록하지 않는다. 그쪽 기록은 job id 로 /jobs/{id}/events 를
    되감기 위한 것인데 MCP 호출자는 job id 를 받지 않아 되감을 수단이 없다 — 방송만
    하면 되고, 안 그러면 도구를 부를 때마다 인메모리 job 이 쌓인다.
    """
    job_id = f"mcp-{uuid.uuid4().hex}"
    activity_bus.job_started(job_id, source="mcp")

    async def emit(event: dict[str, Any]) -> None:
        activity_bus.job_event(job_id, event)

    ok = True
    try:
        return await run_pipeline(emit=emit, **kwargs)
    except Exception:
        ok = False
        raise
    finally:
        # run_pipeline 은 step 이벤트만 내보내고 done 은 호출자 몫이다(jobs.py 와 동일).
        # 여기서 끝내주지 않으면 _active 에 남아 처리현황이 "탐지중" 으로 굳는다.
        activity_bus.job_finished(job_id, ok=ok)


async def _scan_bytes(
    file_bytes: bytes, mime_type: str, file_name: str, *, user_prompt: str = ""
) -> dict[str, Any]:
    result = await _run_pipeline_broadcast(
        file_bytes=file_bytes,
        mime_type=mime_type,
        file_name=file_name,
        wrap_file=False,
        user_prompt=user_prompt or None,
    )
    _record(file_name, result)
    return result


def _record(file_name: str, result: dict[str, Any]) -> None:
    """탐지 통계를 store 에 남긴다(source=mcp). 원문은 저장하지 않음(PLAN §9.1)."""
    try:
        db.record_job(str(uuid.uuid4()), file_name=file_name, source="mcp", result=result)
    except Exception:  # noqa: BLE001 - 통계 기록 실패가 도구 응답을 막지 않게
        pass


# ── 도구 1: scan_text ─────────────────────────────────────────────────────────
@mcp.tool()
async def scan_text(text: str, user_prompt: str = "") -> dict[str, Any]:
    """텍스트에서 개인정보(PII)와 프롬프트 인젝션을 탐지하고 마스킹본을 반환한다.

    사람 확인(HITL) 없이 즉시 탐지 결과를 돌려준다. 신뢰할 수 없는 출처의 텍스트나
    민감정보가 포함될 수 있는 텍스트를 외부 LLM/이메일/다른 문서로 넘기기 전에 먼저 검사하라.

    user_prompt: 이 text 를 검사하게 만든 실제 사용자 요청(있으면). MCP 는 보통
    사용자 요청이 먼저 있고 나서(예: "이 파일 요약해줘") 그 요청에 따라 파일/텍스트를
    읽으므로, 그 요청 문구를 넘기면 인젝션 탐지가 placeholder 대신 이걸 근거로
    "이 텍스트가 사용자의 실제 지시를 무시/변조하려는가"를 판단한다(정확도 향상).

    반환: maskedText, piiItems/injectionItems(각 {type,start,end,confidence,source},
    좌표는 원문 기준 0-based), stats, hasPii/hasInjection, policy.

    원문은 돌려주지 않는다 — 원문(originalText)도, 항목별 원문 값(text)도 없다.
    이 도구를 부르는 쪽이 AI 자신이라, 원문을 주면 검사한 의미가 사라진다.
    마스킹 전 값이 꼭 필요하면 사람이 확인하는 경로(확장의 검토 패널)를 쓸 것.
    """
    result = await _run_pipeline_broadcast(
        text=text, file_name="prompt.txt", wrap_file=False, user_prompt=user_prompt or None
    )
    _record("prompt.txt", result)
    return _public(result)


# ── 도구 2: scan_file ─────────────────────────────────────────────────────────
@mcp.tool()
async def scan_file(file_path: str, mime_type: str = "", user_prompt: str = "") -> dict[str, Any]:
    """로컬 파일을 파서 경유로 검사한다(PII/인젝션 탐지 + 마스킹본).

    TXT/PDF/DOCX 를 지원한다(스캔 PDF 및 HWP/HWPX/XLSX/PPTX 는 v1 미지원 → scanStatus 로
    표시 후 통과, PLAN §9.2). mime_type 을 비우면 확장자로 추정한다. 반환 형식은 scan_text 와
    동일하며 path 필드가 추가된다.

    user_prompt: 이 파일을 읽게 만든 실제 사용자 요청(있으면, scan_text 참고).
    """
    path = _resolve(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

    # 자격증명 파일은 여기서도 막는다 — secure_read_file 만 막아두면 같은 내용을
    # scan_file 의 maskedText 로 그대로 받아갈 수 있어 우회로가 된다.
    if _is_secret_file(path):
        raise PermissionError(f"{_WITHHELD_KINDS['secret']} (path={path})")

    if not mime_type:
        guessed, _ = mimetypes.guess_type(str(path))
        mime_type = guessed or "application/octet-stream"

    file_bytes = path.read_bytes()
    result = await _scan_bytes(file_bytes, mime_type, path.name, user_prompt=user_prompt)
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
        kind = _file_kind(path)
        if kind == "binary":
            skipped.append({"path": str(path), "reason": "unsupported-binary"})
            continue
        if kind == "secret":
            skipped.append({"path": str(path), "reason": "credential-file-withheld"})
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
                    "blocked": scanned["blocked"],
                    "scanStatus": scanned["scanStatus"],
                    "recommendedAction": scanned["recommendedAction"],
                    # block 정책에 걸린 파일은 마스킹본도 넘기지 않는다.
                    "maskedText": "" if scanned["blocked"] else scanned["maskedText"],
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
    PII만 제외)한 뒤 마스킹 결과를 다시 만들고 싶을 때 사용한다.

    반환: {maskedText, applied, skippedCount}. 모양이 어긋난 항목(dict 이 아님,
    type 없음, 좌표가 정수가 아님, 범위 밖)은 조용히 버리고 그 개수를 skippedCount 로
    알려준다 — 이 목록은 호출자가 채워 보내는 값이라, 하나 틀렸다고 요청 전체를
    실패시키는 대신 무엇이 빠졌는지 알려주는 게 맞다.
    """
    raw = list(pii_items or []) + list(injection_items or [])
    items = [it for it in raw if isinstance(it, dict) and isinstance(it.get("type"), str)]
    out = masker.apply_masking(text, items)
    return {
        "maskedText": out["masked_text"],
        "applied": out["applied"],
        "skippedCount": len(raw) - len(out["applied"]),
    }


# ── 도구 5: secure_read_file (§4.2 게이트) ────────────────────────────────────
@mcp.tool()
async def secure_read_file(file_path: str, user_prompt: str = "") -> dict[str, Any]:
    """정책을 적용해 파일을 읽는다 — 항상 파이프라인을 통과한 마스킹본만 반환(PLAN §4.2).

    클라이언트의 기본 Read 도구를 이 도구로 대체하면, 원본이 파이프라인을 우회해
    새어나가는 경로를 없앨 수 있다. 문서/텍스트 파일은 검사 후 maskedText 를 반환한다.
    내용을 아예 돌려주지 않는 경우가 셋 있다(전부 decision="blocked", content=""):
      - 바이너리 파일
      - 자격증명 파일(.env, *.pem, id_rsa 등) — 마스킹은 PII/인젝션만 지우므로
        API 키·비밀번호는 걸러지지 않는다
      - 인젝션이 탐지됐고 정책이 block 인 경우
    decision: masked | clean | blocked.

    user_prompt: 이 파일을 읽게 만든 실제 사용자 요청(있으면, scan_text 참고).
    """
    path = _resolve(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")

    kind = _file_kind(path)
    if kind in _WITHHELD_KINDS:
        return {
            "path": str(path),
            "decision": "blocked",
            "reason": _WITHHELD_KINDS[kind],
            "content": "",
            "blocked": True,
            "policy": {"injection": config.INJECTION_POLICY},
        }

    if not mimetypes.guess_type(str(path))[0] and kind == "text":
        mime = "text/plain"
    else:
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

    result = await _scan_bytes(path.read_bytes(), mime, path.name, user_prompt=user_prompt)
    pub = _public(result)
    # 정책이 block 인데 인젝션이 잡혔으면 내용을 넘기지 않는다. 예전에는 blocked 를
    # 계산해놓고도(orchestrator) 여기서 응답에 싣지조차 않아, block 정책을 켜도 AI
    # 클라이언트는 마스킹본을 그대로 받아갔다 — 정책이 문서에만 있고 이 경로에서는
    # 아무 일도 하지 않았다. 이 도구는 §4.2 게이트라 여기서 막지 않으면 막을 곳이 없다.
    if pub["blocked"]:
        return {
            "path": str(path),
            "decision": "blocked",
            "reason": "인젝션이 탐지되어 차단되었습니다(정책: block).",
            "content": "",
            "blocked": True,
            "scanStatus": pub["scanStatus"],
            "stats": pub["stats"],
            "piiItems": pub["piiItems"],
            "injectionItems": pub["injectionItems"],
            "policy": pub["policy"],
        }
    decision = "masked" if (pub["hasPii"] or pub["hasInjection"]) else "clean"
    return {
        "path": str(path),
        "decision": decision,
        "content": pub["maskedText"],
        "blocked": False,
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

    탐지는 **매칭이 있는 파일당 한 번**만 돈다. 예전에는 매칭 라인마다 run_pipeline 을
    불렀는데, 그건 라인 하나당 PII 인코더 + EXAONE 추론이고 인젝션이 걸리면 Solar
    유료 호출까지 붙는다 — max_results=50 이면 최악 50회 직렬 추론 + 50회 과금이었다.
    파일 전체를 한 번 검사해 좌표를 받은 뒤, 각 라인 구간에 걸친 항목만 그 라인
    기준으로 옮겨 마스킹한다(결과는 라인별로 돌리던 것과 같고, 오히려 라인 경계에
    걸친 항목까지 제대로 잡는다).
    """
    root_path = _resolve(root)
    if not root_path.is_dir():
        raise NotADirectoryError(f"디렉터리가 아닙니다: {root_path}")

    results: list[dict[str, Any]] = []
    for path in sorted(root_path.rglob(pattern)):
        if len(results) >= max_results:
            break
        if not path.is_file():
            continue
        # 자격증명 파일은 검색 스니펫으로도 새지 않게 먼저 걸러낸다(2번과 같은 이유).
        if _file_kind(path) == "secret" or path.suffix.lower() not in _SEARCHABLE_TEXT_EXTS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            try:
                text = path.read_text(encoding="cp949")
            except (UnicodeDecodeError, OSError):
                continue

        # 먼저 매칭 라인을 (원문 기준 좌표와 함께) 모은다. 하나도 없으면 이 파일에는
        # 탐지를 아예 돌리지 않는다 — 검색어가 없는 파일에 추론 비용을 쓸 이유가 없다.
        hits: list[tuple[int, int, int, str]] = []  # (line_no, line_start, line_end, line)
        pos = 0
        for line_no, raw in enumerate(text.splitlines(keepends=True), start=1):
            line = raw.rstrip("\r\n")
            if query in line:
                hits.append((line_no, pos, pos + len(line), line))
            pos += len(raw)
        if not hits:
            continue

        # 파일 전체 1회 검사. 여기는 일부러 방송하지 않는다 — 검색은 "지금 무슨 문서를
        # 처리 중인가" 로 보여줄 단위가 아니다(처리현황이 검색 한 번에 깜빡이게 된다).
        file_res = await run_pipeline(text=text, file_name=path.name, wrap_file=False)
        items = list(file_res.get("piiItems") or []) + list(file_res.get("injectionItems") or [])
        file_blocked = bool(file_res.get("blocked"))

        for line_no, line_start, line_end, line in hits:
            if len(results) >= max_results:
                break
            if file_blocked:
                # block 정책에 걸린 파일은 스니펫도 넘기지 않는다 — 위치는 알려주되
                # 내용은 주지 않는다(그래야 "무엇을 못 봤는지"는 알 수 있다).
                results.append({"path": str(path), "line": line_no,
                                "lineText": "[차단됨 — 인젝션 정책]", "blocked": True})
                continue
            # 이 라인과 겹치는 항목만 골라 라인 기준 좌표로 옮긴다. 라인 밖으로
            # 삐져나간 부분은 잘라낸다(그 부분은 그 라인의 내용이 아니다).
            local: list[dict[str, Any]] = []
            for it in items:
                if it["end"] <= line_start or it["start"] >= line_end:
                    continue
                s = max(it["start"], line_start) - line_start
                e = min(it["end"], line_end) - line_start
                if s < e:
                    # text 는 넘기지 않는다 — 좌표를 잘랐으므로 원래 text 와 길이가
                    # 어긋나고, masker 는 text 가 없으면 좌표를 신뢰한다(validate_and_fix).
                    local.append({"type": it["type"], "start": s, "end": e,
                                  "confidence": it.get("confidence"), "source": it.get("source")})
            masked = masker.apply_masking(line, local)["masked_text"] if local else line
            results.append({"path": str(path), "line": line_no, "lineText": masked, "blocked": False})

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
