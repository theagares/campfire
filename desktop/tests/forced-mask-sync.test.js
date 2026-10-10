'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function load(request, parent, isMain) {
  if (request === 'electron') return { app: { isPackaged: false } };
  return originalLoad.call(this, request, parent, isMain);
};
const { EngineManager } = require('../main/engine-manager');
Module._load = originalLoad;

function bareManager(store) {
  const manager = Object.create(EngineManager.prototype);
  manager.forcedMaskStore = store;
  manager.internalControlToken = 'per-launch-token';
  manager.forcedMaskSyncPromise = null;
  manager.forcedMaskSyncedPort = null;
  manager.boundPort = null;
  manager.lastHealth = null;
  manager.state = 'stopped';
  return manager;
}

test('엔진 동기화는 per-launch 토큰과 JSON 본문을 사용한다', async () => {
  let received;
  const server = http.createServer((req, res) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      received = { method: req.method, url: req.url, auth: req.headers.authorization, body: JSON.parse(body) };
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ managed: true, ready: true, active: true, count: 1, revision: 'opaque' }));
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  try {
    const manager = bareManager({ list: () => ['Project Aurora'] });
    const status = await manager.syncForcedMaskTerms(undefined, port);
    assert.deepEqual(received, {
      method: 'PUT', url: '/internal/forced-mask-rules',
      auth: 'Bearer per-launch-token', body: { terms: ['Project Aurora'] },
    });
    assert.equal(status.count, 1);
    assert.equal(manager.forcedMaskSyncedPort, port);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('암호화 저장 실패 시 실행 중 엔진 정책도 이전 값으로 되돌린다', async () => {
  const calls = [];
  const store = {
    list: () => ['old'],
    replace: () => { throw new Error('disk failed'); },
    limits: () => ({}),
  };
  const manager = bareManager(store);
  manager.state = 'running';
  manager.boundPort = 48200;
  manager.syncForcedMaskTerms = async terms => { calls.push([...terms]); return { ready: true }; };

  await assert.rejects(() => manager.replaceForcedMaskTerms(['new']), /disk failed/);
  assert.deepEqual(calls, [['new'], ['old']]);
});

test('저장된 단어를 못 읽어도 새 목록 저장으로 복구되고, 미동기화 저장은 다음 tick 에 다시 보낸다', async () => {
  let written = null;
  const store = {
    list: () => { throw new Error('키체인 키가 바뀜'); },
    replace: (terms) => { written = terms; },
    limits: () => ({}),
  };
  const manager = bareManager(store);
  manager.state = 'error';            // 엔진이 잠깐 오류 상태 — 지금은 동기화하지 않는다
  manager.forcedMaskSyncedPort = 48200;
  const saved = await manager.replaceForcedMaskTerms(['new']);
  assert.deepEqual(saved.terms, ['new']);
  assert.deepEqual(written, ['new'], '읽을 수 없는 저장소를 덮어쓰지 못했다 — 빠져나갈 길이 없다');
  assert.equal(manager.forcedMaskSyncedPort, null, '동기화 표시가 남아 다음 tick 이 새 단어를 안 보낸다');
});

test('토큰을 모르는(404) 엔진 포트는 더 낮아도 고르지 않는다', async () => {
  const manager = bareManager(null);
  manager.foreignPorts = new Set([48200]);
  manager._probe = async () => ({ service: 'campfire' });
  const found = await manager._scanForEngine();
  assert.equal(found.port, 48201);
});
