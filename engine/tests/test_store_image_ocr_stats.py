from __future__ import annotations

import sqlite3

from app import config
from app.store import db


def _point_store_at(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "STORE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "securedoc.sqlite3")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(config, "LEGACY_STORE_DIR", tmp_path / "legacy")
    monkeypatch.setattr(db, "_conn", None)


def test_old_jobs_table_gets_image_read_count_column(tmp_path, monkeypatch):
    _point_store_at(tmp_path, monkeypatch)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY, file_name TEXT, source TEXT, created_at REAL,
            scan_status TEXT, reason TEXT, blocked INTEGER, injection_policy TEXT,
            pii_count INTEGER, injection_count INTEGER, type_counts TEXT, detections TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO jobs VALUES ('old', 'old.pdf', 'api', 1, 'ok', NULL, 0, 'block', 0, 0, '{}', '[]')"
    )
    conn.commit()
    conn.close()

    db.init_db()
    try:
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(jobs)")}
        assert "image_ocr_read_count" in columns
        assert db._conn.execute(
            "SELECT image_ocr_read_count FROM jobs WHERE job_id='old'"
        ).fetchone() == (0,)
    finally:
        db.close_db()


def test_record_job_persists_only_image_count_and_multiple_images_are_one_document(tmp_path, monkeypatch):
    _point_store_at(tmp_path, monkeypatch)
    result = {
        "scanStatus": "ok",
        "piiItems": [],
        "injectionItems": [],
        "forcedMaskItems": [],
        "images": [
            {"status": "read", "text": "원문을 DB에 저장하면 안 됨"},
            {"status": "read", "text": "두 번째 원문"},
            {"status": "unchecked", "reason": "test"},
        ],
    }

    db.record_job("image-doc", file_name="sample.docx", source="api", result=result)
    try:
        row = db._conn.execute(
            "SELECT image_ocr_read_count, detections, type_counts FROM jobs WHERE job_id=?",
            ("image-doc",),
        ).fetchone()
        assert row == (2, "[]", "{}")
        # 문서 탐지량의 단일 기준: 읽은 그림 수가 1 이상인 job 한 행을 1건으로 센다.
        documents = db._conn.execute(
            "SELECT SUM(CASE WHEN image_ocr_read_count > 0 THEN 1 ELSE 0 END) FROM jobs"
        ).fetchone()[0]
        assert documents == 1
        raw = config.DB_PATH.read_bytes()
        assert "원문을 DB에 저장하면 안 됨".encode() not in raw
        assert "두 번째 원문".encode() not in raw
    finally:
        db.close_db()
