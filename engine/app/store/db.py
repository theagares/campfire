"""
app/store/db.py
SQLite job 메타데이터 + audit.log(JSONL) (PLAN §9.1 / §9.2).

원문 텍스트 영속 저장 절대 금지. 저장하는 것:
  - job 메타: 파일명, 시각, 소스, scan_status, 정책 결과, 탐지 유형별 개수
  - 각 탐지의 start/end/type/source (원문 스니펫 아님)
  - audit.log 도 메타데이터만.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from app import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def init_db() -> None:
    global _conn
    config.STORE_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id        TEXT PRIMARY KEY,
            file_name     TEXT,
            source        TEXT,           -- extension | mcp | prompt | api
            created_at    REAL,
            scan_status   TEXT,           -- ok | failed | unsupported | timeout
            reason        TEXT,
            blocked       INTEGER,
            injection_policy TEXT,
            pii_count     INTEGER,
            injection_count INTEGER,
            type_counts   TEXT,           -- JSON {type: count}
            detections    TEXT            -- JSON [{type,start,end,source}]  (원문 스니펫 없음)
        )
        """
    )
    _conn.commit()


def _type_counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for it in items:
        counts[it["type"]] = counts.get(it["type"], 0) + 1
    return counts


def record_job(job_id: str, *, file_name: str, source: str, result: dict[str, Any]) -> None:
    """job 메타데이터를 저장. 원문/마스킹 텍스트는 저장하지 않는다 (PLAN §9.1)."""
    if _conn is None:
        init_db()
    pii = result.get("piiItems", [])
    inj = result.get("injectionItems", [])
    all_items = list(pii) + list(inj)
    # 탐지 위치/유형/소스만 저장 (원문 스니펫 text 필드 제거)
    detections = [
        {"type": it["type"], "start": it["start"], "end": it["end"], "source": it.get("source")}
        for it in all_items
    ]
    row = (
        job_id,
        file_name,
        source,
        time.time(),
        result.get("scanStatus", "ok"),
        result.get("reason"),
        1 if result.get("blocked") else 0,
        result.get("policy", {}).get("injection", config.INJECTION_POLICY),
        len(pii),
        len(inj),
        json.dumps(_type_counts(all_items), ensure_ascii=False),
        json.dumps(detections, ensure_ascii=False),
    )
    with _lock:
        assert _conn is not None
        _conn.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row
        )
        _conn.commit()

    audit_log(
        {
            "event": "job_recorded",
            "jobId": job_id,
            "fileName": file_name,
            "source": source,
            "scanStatus": result.get("scanStatus", "ok"),
            "blocked": bool(result.get("blocked")),
            "piiCount": len(pii),
            "injectionCount": len(inj),
        }
    )


def audit_log(entry: dict[str, Any]) -> None:
    """audit.log(JSONL) — 메타데이터만, 원문 미포함 (PLAN §9.1)."""
    entry = {"ts": time.time(), **entry}
    try:
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        with config.AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def stats_summary() -> dict[str, Any]:
    if _conn is None:
        init_db()
    assert _conn is not None
    with _lock:
        cur = _conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pii_count),0), COALESCE(SUM(injection_count),0) FROM jobs"
        )
        jobs, pii, inj = cur.fetchone()
    return {"jobs": jobs, "totalPii": pii, "totalInjection": inj}


def close_db() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
