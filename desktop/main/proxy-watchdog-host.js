'use strict';
/**
 * main/proxy-watchdog-host.js
 * 복원 워치독(proxy-watchdog.js)을 detached 로 띄우고 내린다.
 *
 * ELECTRON_RUN_AS_NODE 로 electron 바이너리를 순수 node 처럼 써서 추가 런타임
 * 의존성이 없다. asar 안의 스크립트는 run-as-node 가 못 읽으므로 unpacked 경로로
 * 실행한다(package.json build.asarUnpack 에 proxy-watchdog.js·system-proxy.js 추가).
 *
 * detached + unref 로 앱이 죽어도 살아남게 한다. (앱이 자식을 job object 로 묶어
 * 같이 죽이는지는 기기에서 확인 필요 — 안 되면 예약작업 방식으로 올린다.)
 */

const path = require('path');
const { spawn } = require('child_process');

const SCRIPT = path.join(__dirname, 'proxy-watchdog.js').replace('app.asar', 'app.asar.unpacked');

function create({ scriptPath = SCRIPT, execPath = process.execPath, spawnImpl = spawn } = {}) {
  let child = null;

  function disarm() {
    if (!child) return;
    try { child.kill(); } catch { /* 이미 죽었으면 무시 */ }
    child = null;
  }

  /** @param {{pid:number, pacPrefix:string, previous:object|null}} info */
  function arm(info) {
    disarm(); // 워치독은 하나만 — 재적용 시 이전 것을 먼저 내린다
    const payload = Buffer.from(JSON.stringify({
      pid: info.pid,
      pacPrefix: info.pacPrefix,
      previous: info.previous || null,
    }), 'utf8').toString('base64');
    child = spawnImpl(execPath, [scriptPath, payload], {
      detached: true,          // 앱과 분리 — 앱이 죽어도 계속 산다
      stdio: 'ignore',
      windowsHide: true,
      env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
    });
    // 부모가 이 자식 때문에 종료를 못 하는 일이 없게 참조를 끊는다.
    if (child && typeof child.unref === 'function') child.unref();
    if (child && typeof child.on === 'function') child.on('error', () => { child = null; });
  }

  return { arm, disarm };
}

module.exports = { create, SCRIPT };
