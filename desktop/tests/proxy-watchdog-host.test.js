'use strict';
/**
 * proxy-watchdog-host.js 테스트 (node --test).
 *
 * host 는 워치독을 detached 로 띄우고 내린다. 여기서 지키는 것:
 *  - detached + ELECTRON_RUN_AS_NODE 로 띄운다(앱이 죽어도 살고, electron 을 node 로 씀).
 *  - 인자는 base64(JSON({pid,pacPrefix,previous})) — 그대로 워치독이 읽는다.
 *  - arm 을 다시 부르면 이전 워치독을 먼저 내린다(하나만 유지).
 *  - disarm 은 띄운 프로세스를 kill 한다.
 * 실제 spawn 대신 가짜를 주입한다.
 */

const test = require('node:test');
const assert = require('node:assert');

const { create } = require('../main/proxy-watchdog-host');

function fakeSpawner() {
  const spawned = [];
  const spawnImpl = (execPath, args, opts) => {
    const child = {
      execPath, args, opts, killed: false, unrefed: false,
      unref() { this.unrefed = true; },
      kill() { this.killed = true; },
      on() {},
    };
    spawned.push(child);
    return child;
  };
  return { spawnImpl, spawned };
}

test('arm: detached·run-as-node 로 띄우고 인자를 base64 페이로드로 넘긴다', () => {
  const { spawnImpl, spawned } = fakeSpawner();
  const wd = create({ scriptPath: 'C:/app/proxy-watchdog.js', execPath: 'C:/electron.exe', spawnImpl });

  const prev = { flags: 9, server: null, bypass: null, pacUrl: null };
  wd.arm({ pid: 4321, pacPrefix: 'http://127.0.0.1:48211/campfire.pac', previous: prev });

  assert.equal(spawned.length, 1);
  const c = spawned[0];
  assert.equal(c.execPath, 'C:/electron.exe');
  assert.equal(c.args[0], 'C:/app/proxy-watchdog.js');
  assert.equal(c.opts.detached, true);
  assert.equal(c.opts.env.ELECTRON_RUN_AS_NODE, '1');
  assert.equal(c.unrefed, true);

  const payload = JSON.parse(Buffer.from(c.args[1], 'base64').toString('utf8'));
  assert.deepEqual(payload, { pid: 4321, pacPrefix: 'http://127.0.0.1:48211/campfire.pac', previous: prev });
});

test('arm 두 번: 이전 워치독을 먼저 내린다(하나만)', () => {
  const { spawnImpl, spawned } = fakeSpawner();
  const wd = create({ spawnImpl });
  wd.arm({ pid: 1, pacPrefix: 'p', previous: null });
  wd.arm({ pid: 2, pacPrefix: 'p', previous: null });
  assert.equal(spawned.length, 2);
  assert.equal(spawned[0].killed, true);  // 첫 번째는 내려갔다
  assert.equal(spawned[1].killed, false);
});

test('disarm: 띄운 프로세스를 kill 한다', () => {
  const { spawnImpl, spawned } = fakeSpawner();
  const wd = create({ spawnImpl });
  wd.arm({ pid: 1, pacPrefix: 'p', previous: null });
  wd.disarm();
  assert.equal(spawned[0].killed, true);
  wd.disarm(); // 두 번 불러도 안전
});
