'use strict';
/**
 * main/engine-stats.js
 * 탐지 통계 (PII/INJECTION/기밀/이미지 문서 일·주·월·누적 + 추이) 를 엔진 store 에서 읽는다.
 *
 * 엔진 REST 계약에는 통계 엔드포인트가 없다(/health, /jobs, /jobs/prompt, /jobs/{id}/events 뿐).
 * 엔진 수정은 금지이므로, 앱은 엔진 SQLite store(PLAN §9.1) 를 **read-only** 로 직접 읽는다.
 * Node 22+ 의 실험적 node:sqlite 를 사용(Electron 35+ 가 Node 22 를 번들 — package.json 의
 * "electron": "^35.7.5" 참고). 사용 불가/파일 없음 시 available=false 로 폴백.
 *
 * jobs 테이블(engine/app/store/db.py):
 *   created_at REAL(epoch초), pii_count INTEGER, injection_count INTEGER,
 *   image_ocr_read_count INTEGER, scan_status TEXT ...
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
  // store 는 앱 번들 밖(사용자 데이터 폴더)에 있다 — engineDir 과 무관하다.
  const dbPath = paths.resolveStoreDbPath();

  let db;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
  } catch (err) {
    return { available: false, reason: `store DB 열기 실패: ${err.message}` };
  }

  try {
    // 기밀 탐지(CONFIDENTIAL)는 엔진이 piiItems 에 같이 실어 pii_count 에 섞여 있다 — type_counts(JSON)에서
    // 따로 꺼내 PII 에서 뺀다. 예전 기록에도 같은 규칙이라 그대로 맞는다.
    const columns = db.prepare('PRAGMA table_info(jobs)').all();
    const hasImageReadCount = columns.some((column) => column.name === 'image_ocr_read_count');
    // 엔진이 마이그레이션하기 전의 옛 DB를 앱이 먼저 읽어도 0건으로 안전하게 폴백한다.
    const IMAGE_READ = hasImageReadCount ? 'COALESCE(image_ocr_read_count, 0)' : '0';
    const CONF = "COALESCE(json_extract(type_counts, '$.CONFIDENTIAL'), 0)";
    const SUMS = `COALESCE(SUM(pii_count),0) AS pii, COALESCE(SUM(injection_count),0) AS inj, COALESCE(SUM(${CONF}),0) AS conf, COALESCE(SUM(CASE WHEN ${IMAGE_READ} > 0 THEN 1 ELSE 0 END),0) AS document, COUNT(*) AS jobs`;
    const split = (row) => ({
      pii: (row.pii || 0) - (row.conf || 0),
      inj: row.inj || 0,
      conf: row.conf || 0,
      document: row.document || 0,
      jobs: row.jobs || 0,
    });
    const sumBetween = (since) => split(db.prepare(`SELECT ${SUMS} FROM jobs WHERE created_at >= ?`).get(since));
    const totalRow = split(db.prepare(`SELECT ${SUMS} FROM jobs`).get());

    const today = sumBetween(startOfTodayEpoch());
    const week = sumBetween(daysAgoEpoch(7));
    const month = sumBetween(daysAgoEpoch(30));

    // 추이: 최근 14일 일별 합계
    const trend = [];
    const rows = db
      .prepare(`SELECT created_at, pii_count, injection_count, ${CONF} AS conf, ${IMAGE_READ} AS image_read_count FROM jobs WHERE created_at >= ?`)
      .all(daysAgoEpoch(14));
    const buckets = new Map();
    for (let i = 13; i >= 0; i--) {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      d.setDate(d.getDate() - i);
      const key = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      buckets.set(key, { date: key, pii: 0, injection: 0, confidential: 0, document: 0 });
    }
    for (const r of rows) {
      const d = new Date(r.created_at * 1000);
      d.setHours(0, 0, 0, 0);
      const key = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      const b = buckets.get(key);
      if (b) {
        b.pii += (r.pii_count || 0) - (r.conf || 0);
        b.injection += r.injection_count || 0;
        b.confidential += r.conf || 0;
        b.document += (r.image_read_count || 0) > 0 ? 1 : 0;
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
      confidential: {
        today: today.conf,
        week: week.conf,
        month: month.conf,
        total: totalRow.conf,
      },
      document: {
        today: today.document,
        week: week.document,
        month: month.document,
        total: totalRow.document,
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
