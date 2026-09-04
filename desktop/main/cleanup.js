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
    dir: (root) => paths.resolveModelsDir(root),
    overrideEnv: 'SECUREDOC_MODELS_DIR',
    needsEngineStop: true,
  },
  {
    id: 'stats',
    label: '탐지 기록',
    hint: '대시보드의 탐지 통계와 감사 로그입니다. 지우면 통계가 0부터 다시 쌓입니다.',
    dir: (root) => paths.resolveStoreDir(root),
    overrideEnv: 'SECUREDOC_STORE_DIR',
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
    dir: (root) => paths.resolvePycacheDir(root),
    needsEngineStop: true,
  },
];

/** 엔진이 "옛 기록 승계" 를 건너뛰게 하는 표시. 엔진 쪽 문자열과 반드시 같아야 한다
    (engine/app/store/db.py 의 _adopt_legacy_store). */
const LEGACY_CLEARED_MARKER = '.legacy-store-cleared';

/** "사용자가 탐지 기록을 직접 지웠다" 를 남긴다.
 *
 * 엔진은 기동할 때 DB 가 없으면 앱 번들 안의 옛 기록을 새 위치로 **복사**해온다
 * (리브랜딩 전 통계를 잇기 위한 장치다). 복사라서 번들의 원본이 그대로 남고, 그래서
 * 지울 때마다 다음 기동에 통계와 audit.log 가 되살아난다 — 사용자가 지우라고 한 감사
 * 로그가 돌아오는 건 보안 제품에서 특히 나쁜 실패다. 번들은 건드릴 수 없으므로(쓰면
 * 서명이 깨진다 — paths.js 주석 참고) 새 store 자리에 표시를 남겨 승계를 막는다. */
function markLegacyStoreCleared(storeDir) {
  try {
    fs.mkdirSync(storeDir, { recursive: true });
    fs.writeFileSync(path.join(storeDir, LEGACY_CLEARED_MARKER), '');
    return true;
  } catch {
    // 표시를 못 남겨도 삭제 자체는 성공이다 — 다음 기동에 통계가 되살아날 뿐이다.
    return false;
  }
}

function itemById(id) {
  return ITEMS.find((it) => it.id === id) || null;
}

/** 디렉터리 총 용량(바이트). 접근할 수 없는 항목은 0 으로 세고 넘어간다.
 *
 * **반드시 비동기여야 한다.** 예전엔 readdirSync/statSync 로 훑었는데, 이건 메인
 * 프로세스에서 도는 코드다 — 탐색이 끝날 때까지 모든 창과 엔진 헬스 폴링이 함께 멈춘다.
 * pycache 는 번들 파이썬의 PYTHONPYCACHEPREFIX 목적지라 .pyc 가 수만 개 쌓이는
 * 자리여서 체감이 크다(paths.js resolvePycacheDir 주석 참고). */
async function dirSize(dir) {
  let total = 0;
  let entries = [];
  try {
    entries = await fs.promises.readdir(dir, { withFileTypes: true });
  } catch {
    return 0;
  }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    try {
      if (e.isDirectory()) total += await dirSize(p);
      else if (e.isFile()) total += (await fs.promises.stat(p)).size;
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
async function scan() {
  const root = paths.userDataRoot();
  const items = [];
  for (const it of ITEMS) {
    const dir = it.dir(root);
    const present = exists(dir);
    items.push({
      id: it.id,
      label: it.label,
      hint: it.hint,
      path: dir,
      present,
      bytes: present ? await dirSize(dir) : 0,
      needsEngineStop: it.needsEngineStop,
    });
  }
  return { root, items };
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
  const { engineManager, config, sizes } = ctx;
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
    // 안전장치: ITEMS 를 고치다 실수해도 엉뚱한 곳을 지우지 않는다.
    //  · 기본 경로는 전부 사용자 데이터 루트 안이다 → 그대로 통과
    //  · SECUREDOC_*_DIR 로 위치를 옮긴 항목은 루트 밖일 수 있다 → 그 항목만 허용
    //  · 어느 쪽이든 파일시스템 루트나 사용자 데이터 루트의 상위는 절대 안 된다
    const rel = path.relative(root, dir);
    const insideRoot = !!rel && !rel.startsWith('..') && !path.isAbsolute(rel);
    const overridden = !!t.overrideEnv && !!(process.env[t.overrideEnv] || '').trim();
    const isFsRoot = path.dirname(dir) === dir;
    const back = path.relative(dir, root);
    const holdsRoot = back === '' || (!back.startsWith('..') && !path.isAbsolute(back));
    if (!dir || isFsRoot || holdsRoot || (!insideRoot && !overridden)) {
      removed.push({ id: t.id, ok: false, error: '안전하지 않은 경로라 건너뛰었습니다' });
      continue;
    }
    // 확인 창을 띄우느라 이미 한 번 잰 값이 있으면 그걸 쓴다 — 같은 트리를 두 번
    // 훑을 이유가 없다(600MB 가중치·수만 개 .pyc).
    const before = Object.prototype.hasOwnProperty.call(sizes || {}, t.id)
      ? (sizes[t.id] || 0)
      : (exists(dir) ? await dirSize(dir) : 0);
    try {
      // Windows 는 엔진이 방금 놓은 핸들이 늦게 풀린다(위에서 500ms 를 양보하는 이유가
      // 그것이다). maxRetries 기본값이 0 이라 EBUSY 한 번에 600MB 트리가 반쯤 지워진 채
      // 중단됐다 — 같은 이유로 재시도를 준다.
      fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
      freedBytes += before;
      // 엔진이 다시 뜨면서 번들의 옛 기록을 도로 끌어오지 않게 표시를 남긴다.
      if (t.id === 'stats') markLegacyStoreCleared(dir);
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
