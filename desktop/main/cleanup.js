'use strict';
/**
 * main/cleanup.js
 * 설정 화면의 "데이터 삭제" — 사용자가 항목을 골라 지운다.
 *
 * 설계 원칙 두 가지가 이 파일의 모양을 결정한다.
 *
 * 1) **렌더러는 경로를 정하지 못한다.** IPC 로 오는 건 항목 id 뿐이고, 실제 경로는 여기
 *    ITEMS 표에서만 만들어진다. 렌더러가 경로 문자열을 넘길 수 있게 하면, 렌더러 쪽
 *    버그나 침해 하나로 임의 폴더를 지우는 원격 삭제 통로가 된다.
 * 2) **지우기 전에 본다.** 각 항목의 실제 크기를 재서 보여주고, 확인 창에도 그대로
 *    싣는다. 모델 가중치는 약 600MB 이고 지우면 다시 받아야 하므로, 사용자가 무엇을
 *    잃는지 모르고 누르는 일이 없어야 한다.
 *
 * 엔진이 쓰는 중인 파일(모델 가중치·SQLite)은 엔진을 멈춘 뒤 지우고 원래 상태로
 * 되돌린다. Windows 는 열려 있는 파일을 지울 수 없어서 특히 그렇다.
 */

const fs = require('fs');
const path = require('path');

const paths = require('./paths');

/**
 * 지울 수 있는 것들. id 는 IPC 로 오가는 유일한 식별자다.
 *
 * needsEngineStop: 엔진이 그 파일을 열어둔 채 돌고 있어서, 멈추지 않으면 삭제가
 *   실패하거나(Windows) 엔진이 사라진 파일을 붙들고 이상해진다(mac/linux).
 */
const ITEMS = [
  {
    id: 'models',
    label: '탐지 모델 가중치',
    hint: '지우면 다음 검사 전에 다시 내려받아야 합니다(약 600MB).',
    dir: (root) => path.join(root, 'models'),
    needsEngineStop: true,
  },
  {
    id: 'stats',
    label: '탐지 기록',
    hint: '대시보드의 탐지 통계와 감사 로그입니다. 지우면 통계가 0부터 다시 쌓입니다.',
    dir: (root) => path.join(root, 'store'),
    needsEngineStop: true,
  },
  {
    id: 'logs',
    label: '엔진 로그',
    hint: '문제를 살펴볼 때 쓰는 기록입니다. 지워도 동작에는 영향이 없습니다.',
    dir: (root) => path.join(root, 'logs'),
    needsEngineStop: false,
  },
  {
    id: 'pycache',
    label: 'Python 캐시',
    hint: '엔진이 자동으로 다시 만듭니다. 지운 직후 첫 기동만 조금 느려집니다.',
    dir: (root) => path.join(root, 'pycache'),
    needsEngineStop: true,
  },
];

function itemById(id) {
  return ITEMS.find((it) => it.id === id) || null;
}

/** 디렉터리 총 용량(바이트). 접근할 수 없는 항목은 0 으로 세고 넘어간다. */
function dirSize(dir) {
  let total = 0;
  let entries = [];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return 0;
  }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    try {
      if (e.isDirectory()) total += dirSize(p);
      else if (e.isFile()) total += fs.statSync(p).size;
    } catch {
      // 권한/경합으로 못 읽는 항목은 건너뛴다 — 크기는 안내용이라 정확도보다 견고함이 낫다.
    }
  }
  return total;
}

function exists(p) {
  try { return fs.existsSync(p); } catch { return false; }
}

/** 화면에 뿌릴 목록. 각 항목의 실제 크기를 재서 함께 준다. */
function scan() {
  const root = paths.userDataRoot();
  return {
    root,
    items: ITEMS.map((it) => {
      const dir = it.dir(root);
      const present = exists(dir);
      return {
        id: it.id,
        label: it.label,
        hint: it.hint,
        path: dir,
        present,
        bytes: present ? dirSize(dir) : 0,
        needsEngineStop: it.needsEngineStop,
      };
    }),
  };
}

function formatBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/**
 * 고른 항목을 지운다.
 *
 * @param {string[]} ids 지울 항목 id 목록 (ITEMS 에 없는 값은 조용히 무시한다)
 * @param {{engineManager: any, config: any}} ctx
 * @returns {Promise<{removed: Array, freedBytes: number, engineRestarted: boolean}>}
 */
async function remove(ids, ctx) {
  const { engineManager, config } = ctx;
  const root = paths.userDataRoot();
  const targets = (Array.isArray(ids) ? ids : [])
    .map(itemById)
    .filter(Boolean);
  if (!targets.length) return { removed: [], freedBytes: 0, engineRestarted: false };

  // 엔진이 붙들고 있는 파일은 멈춘 뒤에 지운다. 원래 돌고 있었을 때만 되살린다 —
  // 사용자가 보호를 꺼둔 상태였다면 삭제를 이유로 켜주면 안 된다.
  const wasRunning = !!engineManager && engineManager.getStatus().state !== 'disabled'
    && !!config?.get?.('securityEnabled');
  const mustStop = targets.some((t) => t.needsEngineStop);
  if (mustStop && engineManager) {
    await engineManager.stop();
    // 종료가 파일 핸들에 반영될 시간을 준다(Windows 는 특히 필요하다).
    await new Promise((r) => setTimeout(r, 500));
  }

  const removed = [];
  let freedBytes = 0;
  for (const t of targets) {
    const dir = t.dir(root);
    // 안전장치: 계산된 경로가 사용자 데이터 루트 "안" 인지 다시 확인한다. ITEMS 를
    // 고치다 실수해도 루트 밖을 지우는 일은 없어야 한다.
    const rel = path.relative(root, dir);
    if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) {
      removed.push({ id: t.id, ok: false, error: '안전하지 않은 경로라 건너뛰었습니다' });
      continue;
    }
    const before = exists(dir) ? dirSize(dir) : 0;
    try {
      fs.rmSync(dir, { recursive: true, force: true });
      freedBytes += before;
      removed.push({ id: t.id, ok: true, bytes: before });
    } catch (err) {
      removed.push({ id: t.id, ok: false, error: err.message });
    }
  }

  let engineRestarted = false;
  if (mustStop && engineManager && wasRunning) {
    await engineManager.start();
    engineRestarted = true;
  }

  return { removed, freedBytes, engineRestarted };
}

module.exports = { ITEMS, scan, remove, formatBytes, itemById };
