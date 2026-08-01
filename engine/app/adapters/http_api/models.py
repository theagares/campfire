"""
app/adapters/http_api/models.py
GET  /models/status  — PII/인젝션 실 모델(encoder/llm_mcp) 가중치가 로컬에 있는지 확인.
POST /models/fetch   — models-v1 GitHub Release에서 가중치를 내려받아 압축 해제(백그라운드
                       job, 기존 job_registry/`/jobs/{id}/events` 패턴 재사용해 진행 상황 폴링).

실제 추론 백본(EXAONE-4.0-1.2B, 약 2.4GB)은 이 배포 대상이 아니다 — transformers 가
최초 실행 시 HuggingFace Hub 에서 공개 모델로 알아서 받아 캐싱한다(MODELS.md). 여기서
받는 건 우리가 직접 학습/이식한 작은 아티팩트(PII 인코더 전체 568MB, 인젝션 MLP 헤드
36MB)뿐이다.

주의: 그래서 /models/status 의 injection.ready 는 "MLP 헤드가 있다" 는 뜻이지 "인젝션
탐지가 실제로 돈다" 는 뜻이 아니다. 백본이 캐시에 없으면 헤드가 있어도 서브프로세스가
로딩에 실패한다(실사용자 macOS 신규 설치에서 재현 — llm_mcp.py 의 _backend_model_cached
주석 참고). 백본까지 여기서 배포하려면 model.safetensors 하나가 2.4GB 라 GitHub 릴리스
에셋 상한(2 GiB)을 넘어 분할 업로드가 필요하다.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
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

# local_pii_inference.py(PIIDetector.__init__)/gazetteer.py 가 실제로 읽는 파일들.
# model.safetensors 하나만 확인하던 예전 체크는, 다운로드/압축해제가 중간에
# 끊겨도(엔진 프로세스가 재시작되는 등) model.safetensors 는 있고 label_map.json
# 등은 없는 "일부만 있는" 상태를 그대로 "ready" 로 오판했다(실측: 서브프로세스가
# label_map.json FileNotFoundError 로 즉시 죽음). 필요한 파일을 전부 확인해야
# "정말 완전하게 받아졌다"를 신뢰할 수 있다.
_PII_REQUIRED_FILES = (
    "model.safetensors", "config.json", "label_map.json",
    "gazetteer.json", "tokenizer.json", "tokenizer_config.json",
)
_INJECTION_REQUIRED_FILES = ("model.pt", "calibration.json", "norm_stats.pt")


def _pii_weights_present() -> bool:
    seed_dir = config.PII_ENGINE_DIR / "models" / config.PII_MODEL_SEED
    return all((seed_dir / f).is_file() for f in _PII_REQUIRED_FILES)


def _injection_weights_present() -> bool:
    variant_dir = config.INJECTION_ENGINE_DIR / config.INJECTION_VARIANT
    return all((variant_dir / f).is_file() for f in _INJECTION_REQUIRED_FILES)


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

        # 압축 해제를 dest_dir 에 바로 하지 않고 임시 디렉터리에 전부 끝낸 뒤 옮긴다 —
        # 그래야 중간에 프로세스가 죽어도(엔진 재시작 등) dest_dir 자체는 이전 상태
        # 그대로 남고, "일부 파일만 있는" 상태가 생기지 않는다(위 _pii/_injection
        # _weights_present() 가 이 불변식에 의존한다).
        extract_tmp_dir = Path(tempfile.mkdtemp(prefix=f"securedoc_{name}_extract_"))
        try:
            with tarfile.open(tmp_path, "r:gz") as tar:
                _safe_extract(tar, extract_tmp_dir)
            for item in extract_tmp_dir.iterdir():
                target = dest_dir / item.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                shutil.move(str(item), str(target))
        finally:
            shutil.rmtree(extract_tmp_dir, ignore_errors=True)
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
