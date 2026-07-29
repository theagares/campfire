"""
app/adapters/http_api/models.py
GET  /models/status  — PII/인젝션 실 모델(encoder/llm_mcp) 가중치가 로컬에 있는지 확인.
POST /models/fetch   — models-v1 GitHub Release에서 가중치를 내려받아 압축 해제(백그라운드
                       job, 기존 job_registry/`/jobs/{id}/events` 패턴 재사용해 진행 상황 폴링).

실제 추론 백본(EXAONE-4.0-1.2B)은 이 배포 대상이 아니다 — transformers 가 최초 실행 시
HuggingFace Hub 에서 공개 모델로 알아서 받아 캐싱한다(MODELS.md). 여기서 받는 건 우리가
직접 학습/이식한 작은 아티팩트(PII 인코더 전체 568MB, 인젝션 MLP 헤드 36MB)뿐이다.
"""

from __future__ import annotations

import asyncio
import hashlib
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import APIRouter

from app import config

from . import job_registry

router = APIRouter()

_RELEASE_BASE = "https://github.com/theagares/securedoc-gateway/releases/download/models-v1"

_ASSETS: dict[str, dict[str, Any]] = {
    "pii": {
        "url": f"{_RELEASE_BASE}/pii_engine_v1.tar.gz",
        "sha256": "dbf7d8e52bddc44bea869ca9280ff873875babce7ab7acd2ab67453e9ba7a386",
        "extract_to": lambda: config.PII_ENGINE_DIR,
        "label": "PII 모델(seed42)",
    },
    "injection": {
        "url": f"{_RELEASE_BASE}/injection_engine_v1.tar.gz",
        "sha256": "739b28d517ea2a853bd3fd04d9a2eeeb5afb579b9cfce9fb99283e8afc71a8c3",
        "extract_to": lambda: config.INJECTION_ENGINE_DIR,
        "label": "인젝션 MLP 헤드",
    },
}

# fire-and-forget 백그라운드 태스크가 GC 되지 않도록 참조를 들고 있는다.
_background_tasks: set[asyncio.Task] = set()


def _pii_weights_present() -> bool:
    return (config.PII_ENGINE_DIR / "models" / config.PII_MODEL_SEED / "model.safetensors").is_file()


def _injection_weights_present() -> bool:
    return (config.INJECTION_ENGINE_DIR / config.INJECTION_VARIANT / "model.pt").is_file()


def _status() -> dict[str, Any]:
    return {
        "pii": {"ready": _pii_weights_present()},
        "injection": {"ready": _injection_weights_present()},
    }


@router.get("/models/status")
async def models_status() -> dict[str, Any]:
    return _status()


def _safe_extract(tar: tarfile.TarFile, dest_dir: Path) -> None:
    """path traversal 방지 — 압축 안에 ../ 등으로 dest_dir 밖을 가리키는 경로가 있으면 거부."""
    dest_resolved = dest_dir.resolve()
    for member in tar.getmembers():
        member_path = (dest_dir / member.name).resolve()
        if dest_resolved != member_path and dest_resolved not in member_path.parents:
            raise ValueError(f"압축 파일에 안전하지 않은 경로가 포함되어 있습니다: {member.name}")
    tar.extractall(dest_dir)  # noqa: S202 - 위에서 각 멤버 경로를 이미 검증함


async def _download_and_extract(name: str, spec: dict[str, Any], emit: Callable) -> None:
    await emit({"type": "progress", "asset": name, "label": f"{spec['label']} 다운로드 중...", "pct": 0.0})
    sha256 = hashlib.sha256()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
            tmp_path = Path(tmp.name)
            async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
                async with client.stream("GET", spec["url"]) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0)) or None
                    downloaded = 0
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        tmp.write(chunk)
                        sha256.update(chunk)
                        downloaded += len(chunk)
                        pct = round(downloaded / total * 100, 1) if total else None
                        await emit(
                            {
                                "type": "progress",
                                "asset": name,
                                "label": f"{spec['label']} 다운로드 중...",
                                "pct": pct,
                                "downloadedBytes": downloaded,
                                "totalBytes": total,
                            }
                        )

        digest = sha256.hexdigest()
        if digest != spec["sha256"]:
            raise ValueError(
                f"{spec['label']} 체크섬 불일치(다운로드 손상/변조 의심): "
                f"기대={spec['sha256']} 실제={digest}"
            )

        await emit({"type": "progress", "asset": name, "label": f"{spec['label']} 압축 해제 중...", "pct": 100.0})
        dest_dir: Path = spec["extract_to"]()
        dest_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tmp_path, "r:gz") as tar:
            _safe_extract(tar, dest_dir)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


async def _fetch_job(job_id: str) -> None:
    emit = job_registry.make_emit(job_id)
    try:
        if not _pii_weights_present():
            await _download_and_extract("pii", _ASSETS["pii"], emit)
        if not _injection_weights_present():
            await _download_and_extract("injection", _ASSETS["injection"], emit)
        await emit({"type": "done", "result": _status()})
    except Exception as exc:  # noqa: BLE001 - 실패를 이벤트로 전달(검사 자체를 막지 않음)
        await emit({"type": "error", "message": str(exc)})


@router.post("/models/fetch")
async def fetch_models() -> dict[str, str]:
    """이미 있는 가중치는 재다운로드하지 않는다(멱등). 진행 상황은 기존
    `/jobs/{id}/events`로 폴링(다른 job과 동일한 job_registry 를 공유)."""
    job_id = str(uuid.uuid4())
    job_registry.create_job(job_id)
    task = asyncio.create_task(_fetch_job(job_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"jobId": job_id}
