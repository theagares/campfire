'use strict';
/**
 * main/engine-stats.js
 * 탐지 통계 (PII/INJECTION 일·주·월·누적 + 추이) 를 엔진 store 에서 읽는다.
 *
 * 엔진 REST 계약에는 통계 엔드포인트가 없다(/health, /jobs, /jobs/prompt, /jobs/{id}/events 뿐).
 * 엔진 수정은 금지이므로, 앱은 엔진 SQLite store(PLAN §9.1) 를 **read-only** 로 직접 읽는다.
 * Node 22+ 의 실험적 node:sqlite 를 사용(Electron 35+ 가 Node 22 를 번들 — package.json 의
 * "electron": "^35.7.5" 참고). 사용 불가/파일 없음 시 available=false 로 폴백.
 *
 * jobs 테이블(engine/app/store/db.py):
 *   created_at REAL(epoch초), pii_count INTEGER, injection_count INTEGER, scan_status TEXT ...
 */

const paths = require('./paths');

let DatabaseSync = null;
let sqliteAvailable = false;
try {
  // eslint-disable-next-line global-require
  ({ DatabaseSync } = require('node:sqlite'));
  sqliteAvailable = typeof DatabaseSync === 'function';
} catch {
  sqliteAvailable = false;
}

function startOfTodayEpoch() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

function daysAgoEpoch(n) {
  return Date.now() / 1000 - n * 86400;
}

/**
 * @param {Electron.App} app
 * @returns {{available:boolean, reason?:string, pii?:object, injection?:object, jobs?:number, trend?:Array}}
 */
function readStats(app) {
  if (!sqliteAvailable) {
    return { available: false, reason: 'node:sqlite 미지원 (Node 22+ 필요) — 통계는 플레이스홀더로 표시' };
  }
  const engineDir = paths.resolveEngineDir(app);
  const dbPath = paths.resolveStoreDbPath(engineDir);

  let db;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
  } catch (err) {
    return { available: false, reason: `store DB 열기 실패: ${err.message}` };
  }

  try {
    const sumBetween = (since) => {
      const row = db
        .prepare(
          'SELECT COALESCE(SUM(pii_count),0) AS pii, COALESCE(SUM(injection_count),0) AS inj, COUNT(*) AS jobs ' +
            'FROM jobs WHERE created_at >= ?'
        )
        .get(since);
      return { pii: row.pii || 0, inj: row.inj || 0, jobs: row.jobs || 0 };
    };
    const totalRow = db
      .prepare(
        'SELECT COALESCE(SUM(pii_count),0) AS pii, COALESCE(SUM(injection_count),0) AS inj, COUNT(*) AS jobs FROM jobs'
      )
      .get();

    const today = sumBetween(startOfTodayEpoch());
    const week = sumBetween(daysAgoEpoch(7));
    const month = sumBetween(daysAgoEpoch(30));

    // 추이: 최근 14일 일별 합계
    const trend = [];
    const rows = db
      .prepare(
        'SELECT created_at, pii_count, injection_count FROM jobs WHERE created_at >= ?'
      )
      .all(daysAgoEpoch(14));
    const buckets = new Map();
    for (let i = 13; i >= 0; i--) {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      d.setDate(d.getDate() - i);
      const key = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      buckets.set(key, { date: key, pii: 0, injection: 0 });
    }
    for (const r of rows) {
      const d = new Date(r.created_at * 1000);
      d.setHours(0, 0, 0, 0);
      const key = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      const b = buckets.get(key);
      if (b) {
        b.pii += r.pii_count || 0;
        b.injection += r.injection_count || 0;
      }
    }
    for (const v of buckets.values()) trend.push(v);

    return {
      available: true,
      pii: {
        today: today.pii,
        week: week.pii,
        month: month.pii,
        total: totalRow.pii || 0,
      },
      injection: {
        today: today.inj,
        week: week.inj,
        month: month.inj,
        total: totalRow.inj || 0,
      },
      jobs: totalRow.jobs || 0,
      trend,
    };
  } catch (err) {
    return { available: false, reason: `store 조회 실패: ${err.message}` };
  } finally {
    try { db.close(); } catch { /* noop */ }
  }
}

module.exports = { readStats, sqliteAvailable };
