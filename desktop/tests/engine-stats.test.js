'use strict';
/**
 * main/engine-stats.js — 기밀 탐지(CONFIDENTIAL)는 엔진이 piiItems 에 같이 실어 pii_count 에 섞여 있다.
 * 대시보드는 type_counts 에서 따로 꺼내 PII 와 나눠 보여 줘야 한다 (node --test, 임시 DB).
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

let DatabaseSync = null;
try { ({ DatabaseSync } = require('node:sqlite')); } catch { /* 미지원 Node */ }

test('기밀 건수는 PII 에서 빼서 따로 집계한다', { skip: !DatabaseSync && 'node:sqlite 없음' }, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cf-stats-'));
  const db = new DatabaseSync(path.join(dir, 'securedoc.sqlite3'));
  db.exec('CREATE TABLE jobs (created_at REAL, pii_count INTEGER, injection_count INTEGER, type_counts TEXT)');
  const now = Date.now() / 1000;
  const ins = db.prepare('INSERT INTO jobs VALUES (?, ?, ?, ?)');
  ins.run(now, 18, 1, JSON.stringify({ PERSON_NAME: 4, CONFIDENTIAL: 14, OTHER_INJECTION: 1 }));
  ins.run(now, 3, 0, JSON.stringify({ PHONE: 3 }));                       // 기밀 탐지 없던 문서
  ins.run(now - 40 * 86400, 5, 0, JSON.stringify({ CONFIDENTIAL: 2, EMAIL: 3 })); // 한 달 넘은 기록
  db.close();

  const prev = process.env.SECUREDOC_STORE_DIR;
  process.env.SECUREDOC_STORE_DIR = dir;
  try {
    const { readStats } = require('../main/engine-stats');
    const s = readStats(null);
    assert.equal(s.available, true, s.reason);
    assert.deepEqual(s.confidential, { today: 14, week: 14, month: 14, total: 16 });
    assert.deepEqual(s.pii, { today: 7, week: 7, month: 7, total: 10 });
    assert.equal(s.injection.total, 1);
    assert.deepEqual(s.document, { today: 0, week: 0, month: 0, total: 0 }, '옛 DB는 문서 탐지량 0으로 호환');
    const todayBucket = s.trend[s.trend.length - 1];
    assert.deepEqual([todayBucket.pii, todayBucket.confidential, todayBucket.injection, todayBucket.document], [7, 14, 1, 0]);
  } finally {
    if (prev === undefined) delete process.env.SECUREDOC_STORE_DIR;
    else process.env.SECUREDOC_STORE_DIR = prev;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('읽은 그림 수가 하나 이상인 job을 그림 수와 무관하게 문서 1건으로 센다', { skip: !DatabaseSync && 'node:sqlite 없음' }, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cf-image-stats-'));
  const db = new DatabaseSync(path.join(dir, 'securedoc.sqlite3'));
  db.exec('CREATE TABLE jobs (created_at REAL, pii_count INTEGER, injection_count INTEGER, type_counts TEXT, image_ocr_read_count INTEGER NOT NULL DEFAULT 0)');
  const now = Date.now() / 1000;
  const ins = db.prepare('INSERT INTO jobs VALUES (?, ?, ?, ?, ?)');
  ins.run(now, 0, 0, '{}', 3);                // 그림 3장이어도 문서 1건
  ins.run(now, 0, 0, '{}', 1);                // 문서 1건
  ins.run(now, 0, 0, '{}', 0);                // VARCO가 읽지 않은 문서
  ins.run(now - 40 * 86400, 0, 0, '{}', 2);   // 누적에는 포함, 월간에는 제외
  db.close();

  const prev = process.env.SECUREDOC_STORE_DIR;
  process.env.SECUREDOC_STORE_DIR = dir;
  try {
    const { readStats } = require('../main/engine-stats');
    const s = readStats(null);
    assert.equal(s.available, true, s.reason);
    assert.deepEqual(s.document, { today: 2, week: 2, month: 2, total: 3 });
    assert.equal(s.trend[s.trend.length - 1].document, 2);
  } finally {
    if (prev === undefined) delete process.env.SECUREDOC_STORE_DIR;
    else process.env.SECUREDOC_STORE_DIR = prev;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
