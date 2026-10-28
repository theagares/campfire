'use strict';
/**
 * 홈의 모델 칩 — 엔진이 모델을 올리는 동안(/health 전)에는 기밀 탐지를 켰는지 몰라 줄을 숨겼다가
 * 1분쯤 뒤에야 보였다(Windows UI-1). 그동안은 앱 설정을 따른다.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const Module = require('node:module');
const os = require('node:os');

let version = '0.2.57';
const originalLoad = Module._load;
Module._load = function load(request, parent, isMain) {
  if (request === 'electron') {
    return {
      app: { isPackaged: false, getVersion: () => version, getPath: () => os.tmpdir() },
      ipcMain: {}, BrowserWindow: {}, dialog: {}, shell: {},
    };
  }
  return originalLoad.call(this, request, parent, isMain);
};
const { buildStats } = require('../main/ipc');
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
