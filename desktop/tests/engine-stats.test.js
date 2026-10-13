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
    const todayBucket = s.trend[s.trend.length - 1];
    assert.deepEqual([todayBucket.pii, todayBucket.confidential, todayBucket.injection], [7, 14, 1]);
  } finally {
    if (prev === undefined) delete process.env.SECUREDOC_STORE_DIR;
    else process.env.SECUREDOC_STORE_DIR = prev;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
