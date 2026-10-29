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
import os
import shutil
import sqlite3
import threading
import time
from typing import Any

from app import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _adopt_legacy_store() -> None:
    """예전에 앱 번들 안(APP_DIR/store/data)에 쌓이던 기록을 새 위치로 이어붙인다.

    **옮기지 않고 복사한다.** 모델 가중치 마이그레이션이 shutil.move 를 쓰는 바람에
    개발 체크아웃에서 테스트를 돌리는 것만으로 설치된 앱의 605MB 가중치가 사라진
    사고가 실제로 났다(models_sync._migrate_legacy_models_root 주석 참고). 여기서
    지키려는 건 탐지 통계뿐이라 원본을 지울 이유가 전혀 없고, 남은 사본은 새 위치를
    쓰기 시작한 이상 아무도 읽지 않는다.

    새 위치에 이미 DB 가 있으면 그쪽이 최신이므로 손대지 않는다.
    """
    if os.environ.get("SECUREDOC_SKIP_LEGACY_MIGRATION") == "1":
        return
    # 사용자가 설정 > 데이터 삭제에서 "탐지 기록" 을 직접 지웠다면 승계하지 않는다.
    # 데스크탑 앱이 store 를 지운 뒤 이 표시를 남긴다(main/cleanup.js 의
    # LEGACY_CLEARED_MARKER — 문자열이 양쪽에서 같아야 한다). 표시가 없을 때만
    # 리브랜딩 전 통계를 이어붙인다. 없으면 지운 감사 로그가 다음 기동에 되살아난다.
    if (config.STORE_DIR / ".legacy-store-cleared").exists():
        return
    legacy_db = config.LEGACY_STORE_DIR / config.DB_PATH.name
    if config.DB_PATH.exists() or not legacy_db.is_file():
        return
    try:
        config.STORE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(legacy_db), str(config.DB_PATH))
        legacy_audit = config.LEGACY_STORE_DIR / config.AUDIT_LOG_PATH.name
        if legacy_audit.is_file() and not config.AUDIT_LOG_PATH.exists():
            shutil.copy2(str(legacy_audit), str(config.AUDIT_LOG_PATH))
    except OSError:
        # 권한/볼륨 문제로 못 가져와도 기동을 막지 않는다 — 통계가 0부터 시작할 뿐이다.
        pass


def init_db() -> None:
    global _conn
    config.STORE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _adopt_legacy_store()
    except Exception:  # noqa: BLE001 - 이어붙이기 실패로 엔진을 못 뜨게 하지 않는다
        pass
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
            detections    TEXT,           -- JSON [{type,start,end,source}]  (원문 스니펫 없음)
            image_ocr_read_count INTEGER NOT NULL DEFAULT 0 -- VARCO가 읽은 그림 수(원문 없음)
        )
        """
    )
    # v0.2.63 이하 DB에는 이 칸이 없다. SQLite의 상수 기본값 ADD COLUMN은 기존 행을
    # 다시 쓰지 않으며, 읽기 전용 대시보드는 칸이 생기기 전에도 0으로 폴백한다.
    columns = {row[1] for row in _conn.execute("PRAGMA table_info(jobs)")}
    if "image_ocr_read_count" not in columns:
        _conn.execute(
            "ALTER TABLE jobs ADD COLUMN image_ocr_read_count INTEGER NOT NULL DEFAULT 0"
        )
    _conn.commit()


def _type_counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for it in items:
        counts[it["type"]] = counts.get(it["type"], 0) + 1
    return counts


def _image_ocr_read_count(result: dict[str, Any]) -> int:
    """VARCO가 실제로 읽은 그림 수.

    대시보드의 "문서 탐지량" 기준은 이 값이 1 이상인 job 한 행이다. 그림이 여러 장이어도
    문서 집계는 1건이며, 기준을 바꾸려면 이 함수와 데스크톱의 > 0 집계만 보면 된다.
    """
    images = result.get("images")
    if not isinstance(images, list):
        return 0
    return sum(
        1 for image in images
        if isinstance(image, dict) and image.get("status") == "read"
    )


def record_job(job_id: str, *, file_name: str, source: str, result: dict[str, Any]) -> None:
    """job 메타데이터를 저장. 원문/마스킹 텍스트는 저장하지 않는다 (PLAN §9.1)."""
    if _conn is None:
        init_db()
    pii = result.get("piiItems", [])
    inj = result.get("injectionItems", [])
    forced = result.get("forcedMaskItems", [])
    all_items = list(pii) + list(inj) + list(forced)
    image_ocr_read_count = _image_ocr_read_count(result)
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
        image_ocr_read_count,
    )
    with _lock:
        assert _conn is not None
        _conn.execute(
            """
            INSERT OR REPLACE INTO jobs (
                job_id, file_name, source, created_at, scan_status, reason, blocked,
                injection_policy, pii_count, injection_count, type_counts, detections,
                image_ocr_read_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            row,
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
            "forcedMaskCount": len(forced),
            "imageOcrReadCount": image_ocr_read_count,
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
