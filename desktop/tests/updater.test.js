'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');

const { createUpdater, CHECK_INTERVAL_MS, RELEASES_URL } = require('../main/updater');

function fakeUpdater({ reject = false } = {}) {
  const u = new EventEmitter();
  u.checks = 0;
  u.installs = [];
  u.checkForUpdates = () => {
    u.checks += 1;
    return reject ? Promise.reject(new Error('offline')) : Promise.resolve(null);
  };
  u.quitAndInstall = (...args) => u.installs.push(args);
  return u;
}

function setup(opts = {}) {
  const autoUpdater = fakeUpdater(opts);
  const sent = [];
  const timers = [];
  const updater = createUpdater({
    autoUpdater,
    platform: opts.platform || 'win32',
    broadcast: (ch, payload) => sent.push([ch, payload]),
    every: (fn, ms) => { timers.push({ fn, ms }); return { unref() {} }; },
  });
  return { autoUpdater, sent, timers, updater };
}

test('시작하면 바로 한 번 확인하고, 켜 둔 동안 주기적으로 다시 확인한다', () => {
  const { autoUpdater, timers, updater } = setup();
  updater.start();
  assert.equal(autoUpdater.checks, 1);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, CHECK_INTERVAL_MS);
  timers[0].fn();
  assert.equal(autoUpdater.checks, 2);
  assert.equal(autoUpdater.autoDownload, true);
});

test('받기를 마치면 화면에 알리고, 그 뒤로는 다시 확인하지 않는다', () => {
  const { autoUpdater, sent, timers, updater } = setup();
  updater.start();
  autoUpdater.emit('update-downloaded', { version: '0.2.41' });
  assert.deepEqual(updater.getState(), { ready: true, manual: false, version: '0.2.41', url: null });
  assert.deepEqual(sent.at(-1), ['updater:state', updater.getState()]);
  timers[0].fn();
  assert.equal(autoUpdater.checks, 1); // 이미 받아 둠 — 설치만 남았다
});

test('받아 둔 게 있을 때만 조용히 설치하고 다시 연다', () => {
  const { autoUpdater, updater } = setup();
  assert.equal(updater.install(), false);
  assert.deepEqual(autoUpdater.installs, []);
  autoUpdater.emit('update-downloaded', { version: '0.2.41' });
  assert.equal(updater.install(), true);
  assert.deepEqual(autoUpdater.installs, [[true, true]]);
});

test('macOS 는 받지 않고 내려받을 페이지를 알린다 (자동 설치가 안 됨)', () => {
  const { autoUpdater, updater } = setup({ platform: 'darwin' });
  updater.start();
  assert.equal(autoUpdater.autoDownload, false);
  autoUpdater.emit('update-available', { version: '0.2.41' });
  assert.deepEqual(updater.getState(), { ready: false, manual: true, version: '0.2.41', url: RELEASES_URL });
  assert.equal(updater.install(), false);
});

test('Windows 는 "업데이트 있음" 만으로는 알리지 않는다 (다 받은 뒤에 알림)', () => {
  const { autoUpdater, sent, updater } = setup();
  autoUpdater.emit('update-available', { version: '0.2.41' });
  assert.equal(sent.length, 0);
  assert.equal(updater.getState().ready, false);
});

test('확인이 실패해도(오프라인 등) 앱을 멈추지 않는다', async () => {
  const { autoUpdater, updater } = setup({ reject: true });
  assert.doesNotThrow(() => updater.start());
  await new Promise((r) => setImmediate(r));
  assert.equal(autoUpdater.checks, 1);
});
