'use strict';
/**
 * engine-manager 의 'running' 이벤트 — 헬스 폴링이 2.5초마다 내는 'status' 와 달리
 * running 으로 **바뀔 때만** 난다. 프록시 재적용이 여기에 걸려 있어, 매 tick 나면
 * 실패한 재적용(CA 미신뢰)을 끝없이 반복한다(맥 실측: 분당 24회).
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function load(request, parent, isMain) {
  if (request === 'electron') return { app: { isPackaged: false } };
  return originalLoad.call(this, request, parent, isMain);
};
const { EngineManager } = require('../main/engine-manager');
Module._load = originalLoad;

test('running 이벤트는 running 으로 바뀔 때 한 번만 난다', () => {
  const manager = Object.create(EngineManager.prototype);
  manager.state = 'starting';
  manager.getStatus = () => ({ state: manager.state });
  let running = 0;
  manager.on('running', () => { running += 1; });

  manager._setState('running', null);
  manager._setState('running', null);           // 같은 상태를 다시 알려도
  manager.emit('status', manager.getStatus());  // 폴링 tick 이 status 만 다시 내도
  assert.equal(running, 1);

  manager._setState('starting', '재시작');       // 엔진 재시작 뒤 다시 뜨면 또 한 번
  manager._setState('running', null);
  assert.equal(running, 2);
});

test('맥에서 저장된 단어를 처음 읽기 전에만 키체인 승인 대기를 알린다', async () => {
  const manager = Object.create(EngineManager.prototype);
  manager.state = 'starting';
  manager.forcedMaskStore = { hasStoredTerms: () => true };
  const seen = [];
  manager.on('status', (s) => seen.push(s.keychainWait));
  const platform = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: 'darwin' });
  try {
    await manager._announceKeychainRead();
    await manager._announceKeychainRead(); // 키는 프로세스당 한 번만 키체인에서 읽는다
  } finally {
    Object.defineProperty(process, 'platform', platform);
  }
  assert.deepEqual(seen, [true]);

  const win = Object.create(EngineManager.prototype);
  win.forcedMaskStore = { hasStoredTerms: () => true };
  let emitted = 0;
  win.on('status', () => { emitted += 1; });
  await win._announceKeychainRead();
  assert.equal(emitted, process.platform === 'darwin' ? 1 : 0, '맥이 아니면 알릴 일이 없다');
});
