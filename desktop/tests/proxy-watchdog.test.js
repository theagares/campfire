'use strict';
/**
 * proxy-watchdog.js 테스트 (node --test).
 *
 * 이 프로세스는 앱이 강제종료됐을 때 시스템 프록시를 되돌리는 마지막 안전장치다.
 * 잘못되면 (a) 앱이 살아있는데 되돌려 fail-closed 를 깨거나, (b) 죽었는데 안 되돌려
 * 브라우저를 묶는다. 그래서 "우리 것일 때만 되돌린다" 와 "PID 가 죽을 때까지 기다린다"
 * 를 각각 못 박는다. 실제 시스템 프록시는 건드리지 않는다(가짜 sp).
 */

const test = require('node:test');
const assert = require('node:assert');
const { spawn } = require('node:child_process');

const { isAlive, waitForExit, restoreIfOurs, DIRECT } = require('../main/proxy-watchdog');

test('isAlive: 현재 프로세스는 살아있고, 없는 PID 는 죽음', () => {
  assert.equal(isAlive(process.pid), true);
  assert.equal(isAlive(2147483646), false); // 존재하지 않을 큰 PID
});

test('waitForExit: 자식이 죽으면 곧 true 를 돌려준다', async () => {
  const child = spawn(process.execPath, ['-e', 'setTimeout(()=>{}, 250)'], { stdio: 'ignore' });
  const exited = await waitForExit(child.pid, { pollMs: 50, maxMs: 5000 });
  assert.equal(exited, true);
});

test('waitForExit: 상한을 넘으면 false(앱이 아직 산다 — 손대지 않는다)', async () => {
  let naps = 0;
  const exited = await waitForExit(process.pid, {
    pollMs: 1, maxMs: 5, sleep: () => { naps++; return Promise.resolve(); },
  });
  assert.equal(exited, false);
  assert.ok(naps >= 1);
});

test('restoreIfOurs: 우리 PAC 면 previous 로 되돌린다', async () => {
  const calls = [];
  const sp = {
    query: async () => ({ flags: 4, pacUrl: 'http://127.0.0.1:48211/campfire.pac?v=1' }),
    isOurs: (s, p) => s.pacUrl.startsWith(p),
    restore: async (prev) => { calls.push(prev); },
  };
  const prev = { flags: 9, server: null, bypass: null, pacUrl: null };
  const did = await restoreIfOurs(sp, 'http://127.0.0.1:48211/campfire.pac', prev);
  assert.equal(did, true);
  assert.deepEqual(calls, [prev]);
});

test('restoreIfOurs: 우리 것이 아니면(이미 복원됨/사용자 변경) 아무것도 안 한다', async () => {
  const calls = [];
  const sp = {
    query: async () => ({ flags: 1, pacUrl: null }), // DIRECT
    isOurs: () => false,
    restore: async (prev) => { calls.push(prev); },
  };
  const did = await restoreIfOurs(sp, 'http://127.0.0.1:48211/campfire.pac', { flags: 9 });
  assert.equal(did, false);
  assert.equal(calls.length, 0);
});

test('restoreIfOurs: previous 가 없으면 DIRECT 로 최소한 PAC 는 걷어낸다', async () => {
  const calls = [];
  const sp = {
    query: async () => ({ flags: 4, pacUrl: 'http://127.0.0.1:48211/campfire.pac' }),
    isOurs: () => true,
    restore: async (prev) => { calls.push(prev); },
  };
  await restoreIfOurs(sp, 'http://127.0.0.1:48211/campfire.pac', null);
  assert.deepEqual(calls, [DIRECT]);
});
