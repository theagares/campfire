'use strict';
/**
 * scripts/smoke-engine.js
 * 엔진 사이드카 spawn + /health 포트 탐지 로직 스모크 테스트 (Electron 없이).
 * EngineManager 를 실제 실행해 엔진을 띄우고, 포트를 잡아 /health 를 출력한 뒤 종료한다.
 *
 * 사용: node scripts/smoke-engine.js  (엔진 venv/의존성이 준비돼 있어야 실제 기동됨)
 */

const { EngineManager } = require('../main/engine-manager');
const paths = require('../main/paths');

const stubApp = { isPackaged: false }; // dev 경로 사용
const stubConfig = {
  _d: { securityEnabled: true, injectionPolicy: 'mask' },
  get(k) { return k ? this._d[k] : { ...this._d }; },
  set(p) { Object.assign(this._d, p); return this.get(); },
  setPipelineLayout() {},
};

const engineDir = paths.resolveEngineDir(stubApp);
const pythonExe = paths.resolvePythonExe(engineDir);
console.log('[smoke] engineDir =', engineDir);
console.log('[smoke] pythonExe =', pythonExe);
console.log('[smoke] diagnose  =', paths.diagnose(engineDir, pythonExe));

const mgr = new EngineManager(stubApp, stubConfig);
let done = false;

mgr.on('status', (s) => {
  console.log(`[smoke] status: ${s.state}${s.port ? ' port=' + s.port : ''}${s.message ? ' — ' + s.message : ''}`);
  if (s.state === 'running' && s.port && !done) {
    done = true;
    console.log('[smoke] ✅ 엔진 탐지 성공! /health =', JSON.stringify(s.health, null, 2));
    finish(0);
  }
});

const timeout = setTimeout(() => {
  if (!done) {
    console.error('[smoke] ⏱️  25초 내 엔진을 탐지하지 못했습니다 (엔진 미기동/의존성 문제 가능).');
    finish(1);
  }
}, 25000);

async function finish(code) {
  clearTimeout(timeout);
  console.log('[smoke] 엔진 종료 중…');
  await mgr.dispose();
  setTimeout(() => process.exit(code), 800);
}

mgr.start().catch((e) => {
  console.error('[smoke] start 실패:', e.message);
  finish(1);
});
