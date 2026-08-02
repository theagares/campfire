"""
gen_table.py — 단계별 벤치 결과(JSON)를 마크다운 표로 만든다.

두 문서의 탐지 시간을 합산한 값을 대표값으로 쓰고(문서별 상세는 따로 표로),
직전 단계 대비 / 베이스라인2 대비 배율을 함께 낸다.
"""

from __future__ import annotations

import json
from pathlib import Path

RESULTS = Path(__file__).parent / "results"
API_RESULT = Path(__file__).parent / "results_api.json"

# (파일이름, 표시이름, 커밋, 설명)
STAGES = [
    ("ref_3seed", "(참고) 3-seed 앙상블", "155c9bd~1", "최적화 착수 이전 상태"),
    ("B2_single_seed", "**베이스라인2** 단일 seed", "155c9bd", "3-seed → 단일 모델(seed42)"),
    ("S1_warmup", "+ 워밍업 호출", "72471d8", "로딩 직후 더미 추론 1회"),
    ("S2_solar_cache", "+ Solar 호출 캐싱", "5a0c32b", "exact-match LRU + prompt_cache_key"),
    ("S3_aho_corasick", "+ gazetteer Aho-Corasick", "e0a02b7", "순수 Python 트라이 → C 확장"),
    ("S4_chunk_concurrent", "+ 청크 동시 실행", "72e8699", "순차 await → asyncio.gather"),
    ("S5_hf_offline", "+ HF_HUB_OFFLINE", "e4ce280", "Hub 업데이트 확인 생략"),
    ("S6_crf_cpu", "+ CRF CPU decode", "786e8e3", "Viterbi GPU → CPU numpy"),
    ("HEAD_current", "현재 HEAD", "HEAD", "이후 버그픽스까지 반영"),
]

DOC_LABEL = {0: "카드분쟁접수서", 1: "고객지원리뷰"}


def _load(name: str) -> dict | None:
    p = RESULTS / f"{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _totals(d: dict) -> dict:
    """두 문서 합산."""
    out = {"first_pii": 0.0, "first_inj": 0.0, "warm_pii": 0.0, "warm_inj": 0.0}
    for v in d["docs"].values():
        out["first_pii"] += v["first_pii_ms"]
        out["first_inj"] += v["first_injection_ms"]
        out["warm_pii"] += v["warm_pii_ms"]
        out["warm_inj"] += v["warm_injection_ms"]
    out["first_total"] = out["first_pii"] + out["first_inj"]
    out["warm_total"] = out["warm_pii"] + out["warm_inj"]
    return out


def _fmt_ms(x: float) -> str:
    return f"{x:,.0f}ms" if x < 10000 else f"{x/1000:,.2f}s"


def _speedup(base: float, cur: float) -> str:
    if cur <= 0:
        return "—"
    r = base / cur
    return f"{r:.2f}×"


def main() -> None:
    rows = []
    for name, label, commit, desc in STAGES:
        d = _load(name)
        if d is None:
            continue
        rows.append((label, commit, desc, _totals(d), d))

    api = json.loads(API_RESULT.read_text(encoding="utf-8")) if API_RESULT.exists() else None

    lines: list[str] = []
    lines.append("## 정상 상태(warm) 탐지 시간 — 문서 2건 합산\n")
    lines.append(
        "| 단계 | 커밋 | PII 탐지 | 인젝션 탐지 | 합계 | 직전 대비 | "
        "베이스라인1(API) 대비 | 베이스라인2 대비 |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")

    api_warm = None
    if api is not None:
        a_pii = sum(v["warm_pii_ms"] for v in api["docs"].values())
        a_inj = sum(v["warm_injection_ms"] for v in api["docs"].values())
        api_warm = a_pii + a_inj
        lines.append(
            f"| **베이스라인1** API(Solar Pro 3) | — | {_fmt_ms(a_pii)} | {_fmt_ms(a_inj)} | "
            f"**{_fmt_ms(api_warm)}** | — | 1.00× | — |"
        )

    b2 = next((r for r in rows if r[0].startswith("**베이스라인2")), None)
    b2_warm = b2[3]["warm_total"] if b2 else None
    prev = None
    for label, commit, _desc, t, _d in rows:
        prev_s = _speedup(prev, t["warm_total"]) if prev is not None else "—"
        b2_s = _speedup(b2_warm, t["warm_total"]) if b2_warm else "—"
        api_s = _speedup(api_warm, t["warm_total"]) if api_warm else "—"
        lines.append(
            f"| {label} | `{commit}` | {_fmt_ms(t['warm_pii'])} | {_fmt_ms(t['warm_inj'])} | "
            f"**{_fmt_ms(t['warm_total'])}** | {prev_s} | {api_s} | {b2_s} |"
        )
        prev = t["warm_total"]

    lines.append("\n## 첫 요청(로딩 직후 1회) 탐지 시간 — 문서 2건 합산\n")
    lines.append("| 단계 | 커밋 | PII 탐지 | 인젝션 탐지 | 합계 | 베이스라인2 대비 |")
    lines.append("|---|---|---:|---:|---:|---:|")
    b2_first = b2[3]["first_total"] if b2 else None
    for label, commit, _desc, t, _d in rows:
        b2_s = _speedup(b2_first, t["first_total"]) if b2_first else "—"
        lines.append(
            f"| {label} | `{commit}` | {_fmt_ms(t['first_pii'])} | {_fmt_ms(t['first_inj'])} | "
            f"**{_fmt_ms(t['first_total'])}** | {b2_s} |"
        )

    lines.append("\n## 문서별 상세 (warm)\n")
    lines.append("| 단계 | 문서 | 청크 | PII건 | 인젝션건 | PII ms | 인젝션 ms | 합계 ms |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for label, _commit, _desc, _t, d in rows:
        for i, (_docname, v) in enumerate(d["docs"].items()):
            lines.append(
                f"| {label} | {DOC_LABEL.get(i, i)} | {v['chunks']} | {v['pii_count']} | "
                f"{v['injection_count']} | {v['warm_pii_ms']:,.0f} | {v['warm_injection_ms']:,.0f} | "
                f"{v['warm_total_ms']:,.0f} |"
            )

    out = "\n".join(lines)
    (Path(__file__).parent / "table.md").write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
