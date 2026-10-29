'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const packageJson = require('../package.json');
const { APP_USER_MODEL_ID, configureAppIdentity } = require('../main/app-identity');

test('Windows 알림 AppUserModelID는 설치 appId와 같고 시작 초기에 등록한다', () => {
  const calls = [];
  const app = { setAppUserModelId: (id) => calls.push(id) };

  assert.equal(APP_USER_MODEL_ID, packageJson.build.appId);
  assert.equal(configureAppIdentity(app, 'win32'), true);
  assert.deepEqual(calls, [packageJson.build.appId]);

  const main = fs.readFileSync(path.join(__dirname, '..', 'main', 'main.js'), 'utf8');
  const identityAt = main.indexOf('configureAppIdentity(app)');
  const readyAt = main.indexOf('app.whenReady()');
  assert.ok(identityAt >= 0 && identityAt < readyAt, 'AppUserModelID를 app.whenReady보다 먼저 등록해야 한다');
});

test('Windows가 아니면 AppUserModelID를 건드리지 않는다', () => {
  const app = { setAppUserModelId: () => assert.fail('다른 OS에서 호출하면 안 된다') };
  assert.equal(configureAppIdentity(app, 'darwin'), false);
  assert.equal(configureAppIdentity(app, 'linux'), false);
});
