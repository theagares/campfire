'use strict';
/**
 * 홈의 모델 칩 — 엔진이 모델을 올리는 동안(/health 전)에는 기밀 탐지를 켰는지 몰라 줄을 숨겼다가
 * 1분쯤 뒤에야 보였다(Windows UI-1). 그동안은 앱 설정을 따른다.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const Module = require('node:module');
const os = require('node:os');
const { EventEmitter } = require('node:events');

let version = '0.2.57';
const sent = [];
const originalLoad = Module._load;
Module._load = function load(request, parent, isMain) {
  if (request === 'electron') {
    return {
      app: { isPackaged: false, getVersion: () => version, getPath: () => os.tmpdir() },
      ipcMain: {},
      BrowserWindow: { getAllWindows: () => [{
        isDestroyed: () => false,
        webContents: { send: (channel, payload) => sent.push({ channel, payload }) },
      }] },
      dialog: {}, shell: {},
    };
  }
  return originalLoad.call(this, request, parent, isMain);
};
const { buildStats, bindEngineStatus } = require('../main/ipc');
Module._load = originalLoad;

const confidential = (status, setting) => buildStats({
  getStatus: () => status,
  config: { get: (key) => key === 'confidentialEnabled' && setting },
}).models.confidential.enabled;

test('엔진이 뜨는 중에도 켜 둔 기밀 탐지 칩을 숨기지 않는다', () => {
  assert.equal(confidential({ state: 'starting', health: null }, true), true);
  assert.equal(confidential({ state: 'starting', health: null }, false), false);
  // 엔진이 답하면 엔진이 실제로 켰는지를 따른다
  assert.equal(confidential({ state: 'running', health: { confidential: { enabled: false } } }, true), false);
  assert.equal(confidential({ state: 'running', health: { confidential: { enabled: true } } }, false), true);
  version = '0.2.57-lite.1'; // 라이트 판 엔진은 기밀 탐지를 하지 않는다 — 남은 설정으로 보이면 안 된다
  assert.equal(confidential({ state: 'starting', health: null }, true), false);
});

test('엔진 health가 준비되면 홈 칩용 stats.models도 즉시 갱신한다', () => {
  version = '0.2.57';
  sent.length = 0;
  const manager = new EventEmitter();
  manager.config = { get: (key) => key === 'confidentialEnabled' };
  let status = { state: 'starting', health: null };
  manager.getStatus = () => status;
  const unbind = bindEngineStatus(manager);

  status = {
    state: 'running',
    health: {
      modelsReady: { pii: true, injection: true },
      confidential: { enabled: true, ready: true, kgReady: true },
    },
  };
  manager.emit('status', status);

  const tick = sent.find((item) => item.channel === 'stats:tick');
  assert.equal(tick?.payload?.models?.pii?.ready, true, 'PII 칩이 회색 스냅샷에 머물렀다');
  assert.equal(tick?.payload?.models?.injection?.ready, true, 'Injection 칩이 회색 스냅샷에 머물렀다');
  assert.equal(tick?.payload?.models?.confidential?.ready, true, '기밀 칩이 회색 스냅샷에 머물렀다');
  const ticks = sent.filter((item) => item.channel === 'stats:tick').length;
  manager.emit('status', status);
  assert.equal(
    sent.filter((item) => item.channel === 'stats:tick').length,
    ticks,
    '같은 health 폴링마다 SQLite 통계를 다시 읽고 브로드캐스트했다',
  );
  unbind();
});
