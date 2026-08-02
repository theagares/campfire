"""
bench_local.py — securedoc-gateway 로컬 탐지기(PII encoder + 인젝션 EXAONE)의
"탐지 시간"만 재는 벤치마크.

측정에 포함되는 것: 청크별 PII 탐지, 청크별 인젝션 1차 판정(EXAONE hybrid).
측정에서 제외되는 것:
  - 모델 로딩/서브프로세스 기동(콜드스타트) — 측정 전에 끝내둔다
  - 문서 파싱, 마스킹, 결과 직렬화
  - Solar Pro 3 2단계 위치 특정 — UPSTAGE_API_KEY 를 비워
    config.INJECTION_LOCALIZE_ENABLED=False 로 만들어 원천 차단한다

각 문서마다 두 가지를 따로 낸다:
  first_ms — 로딩 직후 "첫 요청" 1회 (워밍업 개선의 효과가 여기 드러난다)
  warm_ms  — 그 뒤 --repeats 회 반복의 중앙값 (정상 상태 처리 속도)

engine 디렉터리는 --engine-dir 로 받는다(각 개선 단계 커밋으로 checkout 된 트리).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path


def _prepare_env(args: argparse.Namespace) -> None:
    """detector 가 읽는 설정을 환경변수로 고정한다. app 임포트 전에 끝내야 한다."""
    os.environ["SECUREDOC_MODELS_DIR"] = args.models_dir
    os.environ["SECUREDOC_PII_DETECTOR"] = "encoder"
    os.environ["SECUREDOC_INJECTION_DETECTOR"] = "llm_mcp"
    os.environ["SECUREDOC_PII_DEVICE"] = args.device
    os.environ["SECUREDOC_INJECTION_DEVICE"] = args.device
    # Solar 2단계 위치 특정을 끈다 — 이 벤치의 측정 대상이 아니다.
    # config 는 UPSTAGE_API_KEY 가 비면 INJECTION_LOCALIZE_ENABLED=False 로 둔다.
    os.environ["UPSTAGE_API_KEY"] = ""
    os.environ["SECUREDOC_UPSTAGE_API_KEY"] = ""
    # 서브프로세스가 쓰는 파이썬을 이 벤치 venv 로 고정(엔진 기본값은 sys.executable).
    os.environ["SECUREDOC_PII_PYTHON_EXECUTABLE"] = sys.executable
    os.environ["SECUREDOC_INJECTION_PYTHON_EXECUTABLE"] = sys.executable


def _clear_stage_runtime(models_dir: Path) -> None:
    """이전 단계의 런타임 코드/바이트코드가 남아 다음 단계에 섞이지 않게 지운다.
    가중치(models/hybrid/attn)는 junction 이라 건드리지 않는다."""
    for engine_name in ("pii_engine", "injection_engine"):
        rt = models_dir / engine_name / "runtime"
        if rt.exists():
            shutil.rmtree(rt, ignore_errors=True)


# 가중치 원본(사용자 기기에 설치된 것) — 어느 레이아웃이든 여기를 가리키게 한다.
REAL_WEIGHTS = Path(os.environ.get("LOCALAPPDATA", "")) / "UpSecurity" / "models"

# (엔진 하위 경로, 가중치 디렉터리 이름)
_WEIGHT_LINKS = (
    ("pii_engine", "models"),
    ("injection_engine", "hybrid"),
    ("injection_engine", "attn"),
)


def _is_legacy_layout(engine_dir: Path) -> bool:
    """models_sync.py 가 없는 옛 커밋은 가중치를 엔진 트리 안에서 찾는다
    (config.PII_ENGINE_DIR = APP_DIR/models/pii_engine). 가중치를 앱 설치 폴더
    밖으로 옮긴 건 나중 커밋이라, 단계별 벤치는 두 레이아웃을 모두 다뤄야 한다."""
    return not (engine_dir / "app" / "models_sync.py").exists()


def _link_weights_in_tree(engine_dir: Path) -> None:
    """옛 레이아웃용: 엔진 트리 안 기대 위치에 실제 가중치로의 junction 을 건다.
    가중치는 gitignore 대상이라 checkout 해도 지워지지 않고 그대로 남는다."""
    base = engine_dir / "app" / "models"
    for engine_name, weight_name in _WEIGHT_LINKS:
        link = base / engine_name / weight_name
        target = REAL_WEIGHTS / engine_name / weight_name
        if link.exists() or not target.is_dir():
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        # cmd.exe 의 mklink 는 한글 경로에서 코드페이지 문제로 실패한다(실측) —
        # 인자를 UTF-16 으로 넘기는 PowerShell 로 junction 을 만든다.
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f'New-Item -ItemType Junction -Path "{link}" -Target "{target}" | Out-Null',
            ],
            capture_output=True,
        )


async def _load_text(engine_dir: Path, doc: Path) -> str:
    from app.core.parser import STATUS_OK, parse_document

    raw = doc.read_bytes()
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    text, status, reason = parse_document(raw, mime, doc.name)
    if status != STATUS_OK:
        raise RuntimeError(f"{doc.name}: 파싱 실패 status={status} reason={reason}")
    return text


async def _run(args: argparse.Namespace) -> dict:
    from app import config
    from app.core.detectors.injection import llm_mcp
    from app.core.detectors.pii import encoder
    from app.core.pipeline.orchestrator import _detect_all, _split_chunks

    if not _is_legacy_layout(Path(args.engine_dir)):
        # 새 레이아웃: 이 단계 커밋의 runtime/*.py 를 스테이징 모델 디렉터리로
        # 복사한다(가중치는 제외 — junction 으로 이미 연결돼 있다).
        from app import models_sync

        models_sync.sync_bundled_model_files()
    # 옛 레이아웃은 runtime/*.py 가 엔진 트리 안에 그대로 있으므로 동기화가 필요 없다.

    assert not config.INJECTION_LOCALIZE_ENABLED, "Solar 2단계가 꺼져 있어야 한다"

    docs = [Path(d) for d in args.docs]
    texts = {}
    for d in docs:
        texts[d.name] = await _load_text(Path(args.engine_dir), d)

    pii_det = encoder.build()
    inj_det = llm_mcp.build()

    # ── 로딩(콜드스타트)은 측정 대상이 아니다: 여기서 끝내둔다 ──────────────
    load_t0 = time.perf_counter()
    await pii_det._ensure_process()
    await inj_det._ensure_process()
    load_sec = time.perf_counter() - load_t0

    results: dict = {
        "stage": args.stage,
        "commit": args.commit,
        "device": args.device,
        "load_sec": round(load_sec, 3),
        "docs": {},
    }

    for name, text in texts.items():
        chunks = _split_chunks(text, config.CHUNK_SIZE)

        async def once() -> tuple[float, float, int, int]:
            t0 = time.perf_counter()
            pii = await _detect_all(pii_det, text, chunks)
            t1 = time.perf_counter()
            inj = await _detect_all(inj_det, text, chunks)
            t2 = time.perf_counter()
            return (t1 - t0) * 1000, (t2 - t1) * 1000, len(pii), len(inj)

        # 첫 요청 1회 — 워밍업 개선이 드러나는 자리.
        f_pii, f_inj, n_pii, n_inj = await once()

        warm_pii: list[float] = []
        warm_inj: list[float] = []
        for _ in range(args.repeats):
            w_pii, w_inj, n_pii, n_inj = await once()
            warm_pii.append(w_pii)
            warm_inj.append(w_inj)

        results["docs"][name] = {
            "chars": len(text),
            "chunks": len(chunks),
            "pii_count": n_pii,
            "injection_count": n_inj,
            "first_pii_ms": round(f_pii, 1),
            "first_injection_ms": round(f_inj, 1),
            "first_total_ms": round(f_pii + f_inj, 1),
            "warm_pii_ms": round(statistics.median(warm_pii), 1),
            "warm_injection_ms": round(statistics.median(warm_inj), 1),
            "warm_total_ms": round(statistics.median(warm_pii) + statistics.median(warm_inj), 1),
            "warm_pii_all": [round(x, 1) for x in warm_pii],
            "warm_injection_all": [round(x, 1) for x in warm_inj],
        }

    for det in (pii_det, inj_det):
        proc = getattr(det, "_process", None)
        if proc is not None and proc.returncode is None:
            proc.terminate()
    await asyncio.sleep(0.5)
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--commit", default="")
    ap.add_argument("--engine-dir", required=True)
    ap.add_argument("--models-dir", required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--docs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    _clear_stage_runtime(Path(args.models_dir))
    if _is_legacy_layout(Path(args.engine_dir)):
        _link_weights_in_tree(Path(args.engine_dir))
    _prepare_env(args)
    sys.path.insert(0, args.engine_dir)

    results = asyncio.run(_run(args))
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
