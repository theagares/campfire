"""
bench_api.py — "PII 탐지도 인젝션 탐지도 전부 외부 LLM API로 하던 방식"의
기초 베이스라인(베이스라인1) 측정.

이 리포지토리에는 API 기반 PII 탐지기가 없다(PII 는 처음부터 로컬 encoder). 그래서
"만약 둘 다 API로 했다면" 을 재현하려고 이 스크립트를 따로 둔다. 프롬프트는 새로
지어내지 않고, 팀이 실제로 그 목적으로 써 뒀던
`파이프라인/server/pipeline/prompts.py` 의 PII_SYSTEM / INJECTION_SYSTEM 을 그대로
읽어 쓴다(--prompts 로 경로 지정).

구성: 청크(1,000자/겹침 100자)마다 Solar Pro 3 를 2번 호출(PII용 1회, 인젝션용 1회).
로컬 파이프라인과 동일하게 청크들은 동시 실행한다. 캐시는 쓰지 않는다 — 베이스라인
이므로(캐싱은 이후 개선사항으로 따로 측정된다).

측정 대상은 bench_local.py 와 동일하게 "탐지 시간"뿐이다: 문서 파싱·마스킹은 제외.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

API_BASE = "https://api.upstage.ai/v1/solar/chat/completions"
MODEL = "solar-pro3"


def _load_prompts(prompts_py: Path) -> tuple[str, str]:
    """프로젝트가 이미 갖고 있는 시스템 프롬프트를 그대로 읽어 쓴다."""
    ns: dict = {}
    exec(compile(prompts_py.read_text(encoding="utf-8"), str(prompts_py), "exec"), ns)
    return ns["PII_SYSTEM"], ns["INJECTION_SYSTEM"]


def _split_chunks(text: str, chunk_size: int = 1000, overlap: int = 100) -> list[dict]:
    """orchestrator._split_chunks 와 동일한 규칙(비교 가능하게 맞춘다)."""
    if not text:
        return [{"text": "", "offset": 0}]
    chunks: list[dict] = []
    step = max(1, chunk_size - overlap)
    i, n = 0, len(text)
    while i < n:
        chunks.append({"text": text[i : i + chunk_size], "offset": i})
        if i + chunk_size >= n:
            break
        i += step
    return chunks


def _read_key(env_path: Path) -> str:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        k, _, v = line.strip().partition("=")
        if k.strip() == "upstage_key":
            return v.strip()
    raise RuntimeError(f"upstage_key 를 {env_path} 에서 찾지 못함")


async def _call_once(client: httpx.AsyncClient, key: str, system: str, chunk: str) -> list[dict]:
    resp = await client.post(
        API_BASE,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": chunk},
            ],
            "temperature": 0,
        },
        # solar-pro3 는 한 청크에서 PII 를 전부 뽑는 데 수 초~수십 초가 걸린다
        # (실측: 기본 60초로 두면 ReadTimeout 발생) — 넉넉히 잡는다.
        timeout=httpx.Timeout(300.0, connect=15.0),
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0)).get("items", []) or []
    except json.JSONDecodeError:
        return []


async def _call(
    client: httpx.AsyncClient, key: str, system: str, chunk: str, attempts: int = 3
) -> list[dict]:
    """일시적 실패(타임아웃/429/5xx)는 재시도한다 — 한 번의 네트워크 사고로
    전체 측정이 날아가지 않게."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await _call_once(client, key, system, chunk)
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as e:
            last = e
            if i < attempts - 1:
                await asyncio.sleep(2.0 * (i + 1))
    raise RuntimeError(f"Solar 호출이 {attempts}회 모두 실패: {last!r}")


async def _detect_all(client, key, system, chunks) -> int:
    """청크별 API 호출을 동시에 실행(로컬 파이프라인의 _detect_all 과 같은 구조)."""
    lists = await asyncio.gather(*(_call(client, key, system, ch["text"]) for ch in chunks))
    return sum(len(x) for x in lists)


async def _run(args) -> dict:
    pii_system, inj_system = _load_prompts(Path(args.prompts))
    key = _read_key(Path(args.env_file))

    sys.path.insert(0, args.engine_dir)
    from app.core.parser import STATUS_OK, parse_document

    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    results: dict = {"stage": args.stage, "model": MODEL, "docs": {}}

    async with httpx.AsyncClient() as client:
        for doc_path in args.docs:
            doc = Path(doc_path)
            text, status, reason = parse_document(doc.read_bytes(), mime, doc.name)
            if status != STATUS_OK:
                raise RuntimeError(f"{doc.name}: 파싱 실패 {status} {reason}")
            chunks = _split_chunks(text)

            pii_ms: list[float] = []
            inj_ms: list[float] = []
            n_pii = n_inj = 0
            for _ in range(args.repeats):
                t0 = time.perf_counter()
                n_pii = await _detect_all(client, key, pii_system, chunks)
                t1 = time.perf_counter()
                n_inj = await _detect_all(client, key, inj_system, chunks)
                t2 = time.perf_counter()
                pii_ms.append((t1 - t0) * 1000)
                inj_ms.append((t2 - t1) * 1000)

            results["docs"][doc.name] = {
                "chars": len(text),
                "chunks": len(chunks),
                "pii_count": n_pii,
                "injection_count": n_inj,
                "warm_pii_ms": round(statistics.median(pii_ms), 1),
                "warm_injection_ms": round(statistics.median(inj_ms), 1),
                "warm_total_ms": round(
                    statistics.median(pii_ms) + statistics.median(inj_ms), 1
                ),
                "warm_pii_all": [round(x, 1) for x in pii_ms],
                "warm_injection_all": [round(x, 1) for x in inj_ms],
            }
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="B1_api")
    ap.add_argument("--engine-dir", required=True)
    ap.add_argument("--prompts", required=True, help="PII_SYSTEM/INJECTION_SYSTEM 이 든 prompts.py")
    ap.add_argument("--env-file", required=True, help="upstage_key=... 가 든 .env")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--docs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    results = asyncio.run(_run(args))
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
