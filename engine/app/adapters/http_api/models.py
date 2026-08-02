"""
app/adapters/http_api/models.py
GET  /models/status  — PII/인젝션 실 모델(encoder/llm_mcp) 가중치가 로컬에 있는지 확인.
POST /models/fetch   — models-v1 GitHub Release에서 가중치를 내려받아 압축 해제(백그라운드
                       job, 기존 job_registry/`/jobs/{id}/events` 패턴 재사용해 진행 상황 폴링).

우리가 직접 학습/이식한 작은 아티팩트(PII 인코더 568MB, 인젝션 MLP 헤드 36MB)는 우리
GitHub 릴리스(models-v1)에서 받고, 추론 백본(EXAONE-4.0-1.2B, 약 2.4GB)은 HuggingFace
에서 받는다 — model.safetensors 하나가 2.4GB 라 GitHub 릴리스 에셋 상한(2 GiB)을 넘어
우리가 직접 호스팅할 수 없기 때문이다.

(2026-08-01 재정정) 예전엔 백본을 여기서 다루지 않고 "실제 검사 시점에 transformers 가
알아서 받겠지" 하고 두었다. 그 결과 두 가지가 깨졌다:
  1. injection.ready 가 "MLP 헤드가 있다" 는 뜻인데 앱은 "인젝션 탐지가 돈다" 로 믿었다.
     백본이 없으면 헤드가 있어도 서브프로세스가 로딩에 실패한다(실사용자 macOS 신규
     설치에서 재현).
  2. 받더라도 검사 도중 몇 분간 아무 표시 없이 멈춘 것처럼 보였다.
이제 백본도 이 흐름에서 받고(_download_backbone) 진행 상황을 같은 job 이벤트로 방출하며,
injection.ready 는 헤드와 백본이 모두 있을 때만 참이다.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from app.core.detectors.injection import backbone

from . import job_registry

router = APIRouter()

_RELEASE_BASE = "https://github.com/theagares/securedoc-gateway/releases/download/models-v1"

# v2 는 seed42 하나만 담는다. v1 은 seed42/43/44 를 담고 있었는데, 세 seed 가 실제로는
# 동일한 가중치라 tar 안에서 하드링크로 묶여 있었다 — 그래서 압축본은 476MB 로 작았지만
# Windows 에서 풀면 하드링크가 실제 복사본으로 펼쳐져 디스크에 1.7GB 를 차지했다(실측).
# 어차피 단일 seed 전환(155c9bd) 이후 읽는 건 seed42 뿐이라, 나머지 둘은 순수 낭비였다.
# 다운로드 크기는 v1 과 같고(476MB), 압축 해제 후 디스크 사용량만 1.7GB → 569MB 로 준다.
# v1 은 지우지 않고 남겨둔다 — 이미 배포된 구버전 앱이 그 URL/체크섬을 그대로 쓴다.
_ASSETS: dict[str, dict[str, Any]] = {
    "pii": {
        "url": f"{_RELEASE_BASE}/pii_engine_v2.tar.gz",
        "sha256": "1cc79978799d1d6c3a7b1737bed4e407ebfe3a6fc0925e73c7d18da21a1d3032",
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


def _injection_head_present() -> bool:
    variant_dir = config.INJECTION_ENGINE_DIR / config.INJECTION_VARIANT
    return all((variant_dir / f).is_file() for f in _INJECTION_REQUIRED_FILES)


def _injection_weights_present() -> bool:
    """인젝션 탐지가 실제로 뜰 수 있는 상태인가.

    MLP 헤드만으로는 부족하다 — 백본(EXAONE 2.4GB)이 없으면 헤드가 있어도 서브프로세스가
    로딩에 실패한다(실사용자 macOS 신규 설치에서 재현). 예전엔 헤드만 보고 ready 를
    돌려줘서, 앱은 "준비됨" 이라 믿고 아무것도 안 받았고 정작 검사할 때 죽었다.
    """
    return _injection_head_present() and backbone.is_cached()


def _status() -> dict[str, Any]:
    return {
        "pii": {"ready": _pii_weights_present()},
        # head/backbone 은 어느 쪽이 비었는지 UI·로그가 구분할 수 있게 덧붙인 정보다
        # (기존 소비자는 ready 만 본다).
        "injection": {
            "ready": _injection_weights_present(),
            "head": _injection_head_present(),
            "backbone": backbone.is_cached(),
        },
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


async def _download_backbone(emit: Callable) -> None:
    """인젝션 백본(EXAONE 약 2.4GB)을 HuggingFace 에서 받으며 진행 상황을 방출한다.

    예전엔 이걸 받지 않고 두었다가, 실제 검사 시점에 서브프로세스 안의
    from_pretrained() 가 알아서 받게 했다 — 그러면 앱은 아무 표시도 못 하고 그냥
    몇 분간 멈춘 것처럼 보인다(캐시가 없으면 아예 실패했다). 다른 가중치와 같은
    /models/fetch 흐름에서 받아 기존 진행 표시 UI 를 그대로 태운다.

    진행률은 다운로드 스레드가 올리는 카운터를 이벤트 루프 쪽에서 주기적으로 읽어
    방출한다 — 청크마다 스레드에서 코루틴을 깨우지 않기 위해서다.
    """
    label = "인젝션 백본(EXAONE)"
    await emit({"type": "progress", "asset": "backbone", "label": f"{label} 준비 중...", "pct": 0.0})

    total = await asyncio.to_thread(backbone.total_download_bytes)
    done = asyncio.Event()

    async def report() -> None:
        while not done.is_set():
            # 진행률은 디스크에 실제로 쌓인 양으로 계산한다 — tqdm 훅을 가로채는 방식은
            # huggingface_hub 버전·다운로드 백엔드에 따라 아예 안 불려서 진행률이 계속
            # 0% 로 머물렀다(실사용자 macOS 재현).
            #
            # 퍼센트로만 보고한다(앱 UI 가 label + pct% 를 그린다). 총 크기를 못 구했으면
            # pct 는 None 으로 두고 라벨만 보여준다 — 모르는 값을 그럴듯한 숫자로 지어내지
            # 않는다(system-metrics.js 의 "허위 수치 금지" 와 같은 원칙).
            # "받아야 할 전체 대비 지금 디스크에 있는 양" 으로 잡는다. 중단 후 재시도라
            # 일부가 이미 있으면 그만큼에서 시작해 100% 로 간다(0 부터 다시 세면 끝까지
            # 채워도 100% 에 못 미친다).
            pct = None
            if total:
                got = await asyncio.to_thread(backbone.downloaded_bytes)
                pct = min(round(got / total * 100, 1), 100.0)
            await emit(
                {
                    "type": "progress",
                    "asset": "backbone",
                    "label": f"{label} 다운로드 중...",
                    "pct": pct,
                }
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), timeout=1.0)

    reporter = asyncio.create_task(report())
    try:
        await asyncio.to_thread(backbone.download)
    finally:
        done.set()
        await reporter
    await emit({"type": "progress", "asset": "backbone", "label": f"{label} 완료", "pct": 100.0})


async def _fetch_job(job_id: str) -> None:
    emit = job_registry.make_emit(job_id)
    try:
        if not _pii_weights_present():
            await _download_and_extract("pii", _ASSETS["pii"], emit)
        if not _injection_head_present():
            await _download_and_extract("injection", _ASSETS["injection"], emit)
        if not backbone.is_cached():
            await _download_backbone(emit)
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
