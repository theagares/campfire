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
