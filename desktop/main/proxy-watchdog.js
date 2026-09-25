'use strict';
/**
 * main/proxy-watchdog.js
 * 앱이 죽으면(정상이든 강제든) 시스템 프록시를 되돌리는 감시 프로세스.
 *
 * 왜 별도 프로세스인가: cleanup()/suspend() 는 정상 종료(will-quit)에서만 돈다.
 * 작업관리자 끝내기·크래시로 **강제종료**되면 아무 정리 코드도 안 돌아, 브라우저가
 * 죽은 PAC 에 묶인다(그다음 실행의 recoverOnLaunch 전까지 계속). 이 프로세스는 앱과
 * 분리돼(detached) 떠 있다가 앱 PID 가 사라지는 순간 깨어나, 프록시가 아직 "우리
 * 것" 이면 proxySystemPrevious 로 되돌린다.
 *
 * fail-closed 를 안 깬다: 엔진 재시작은 앱(=이 PID)을 안 죽이므로 안 깨어난다.
 * 정상 종료 땐 cleanup 이 먼저 복원하므로, 깨어나서 봐도 "우리 것 아님" → no-op.
 *
 * 커버 못 하는 경우(이 워치독까지 강제종료되거나 전원 차단)는 다음 실행의
 * recoverOnLaunch 가 backstop 이다 — 그래서 그건 그대로 둔다.
 *
 * 실행: electron 을 ELECTRON_RUN_AS_NODE 로 detached 실행(proxy-watchdog-host.js).
 * 인자: base64(JSON({ pid, pacPrefix, previous })).
 */

// PROXY_TYPE_DIRECT. previous 를 못 받았으면 최소한 우리 PAC 는 걷어낸다.
const DIRECT = { flags: 1, server: null, bypass: null, pacUrl: null };

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === 'EPERM'; // 살아있지만 신호 권한이 없는 경우
  }
}

/** pid 가 죽을 때까지 기다린다(폴링). 상한을 넘기면 false(아직 살아있음). */
async function waitForExit(pid, { pollMs = 1000, maxMs = 30 * 24 * 3600 * 1000, sleep } = {}) {
  const nap = sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const deadline = Date.now() + maxMs;
  while (isAlive(pid) && Date.now() < deadline) await nap(pollMs);
  return !isAlive(pid);
}

/** 프록시가 아직 우리 것이면 previous 로 되돌린다. 되돌렸으면 true. */
async function restoreIfOurs(sp, pacPrefix, previous) {
  const state = await sp.query();
  if (!sp.isOurs(state, pacPrefix)) return false; // 이미 복원됐거나 사용자가 바꿨다
  await sp.restore(previous || DIRECT);
  return true;
}

async function main() {
  let cfg;
  try {
    cfg = JSON.parse(Buffer.from(process.argv[2] || '', 'base64').toString('utf8'));
  } catch {
    process.exit(2);
  }
  if (!cfg || !cfg.pid || !cfg.pacPrefix) process.exit(2);

  const exited = await waitForExit(cfg.pid);
  if (!exited) process.exit(0); // 상한 도달 — 앱이 아직 산다. 손대지 않는다.

  const { create } = require('./system-proxy');
  try {
    const restored = await restoreIfOurs(create(), cfg.pacPrefix, cfg.previous);
    console.log(restored
      ? '[watchdog] 앱 종료 감지 — 시스템 프록시 복원함'
      : '[watchdog] 앱 종료 감지 — 프록시가 우리 것이 아님(이미 복원됨) → no-op');
    process.exit(0);
  } catch (e) {
    console.error('[watchdog] 복원 실패:', e.message);
    process.exit(1);
  }
}

if (require.main === module) main();

module.exports = { isAlive, waitForExit, restoreIfOurs, DIRECT };
