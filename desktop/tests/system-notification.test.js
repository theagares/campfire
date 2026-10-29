'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createFileLogger, showSystemNotification } = require('../main/system-notification');

class FakeNotification extends EventEmitter {
  static supported = true;
  static instances = [];

  static isSupported() { return this.supported; }

  constructor(options) {
    super();
    this.options = options;
    this.shown = false;
    FakeNotification.instances.push(this);
  }

  show() {
    this.shown = true;
    this.emit('show');
  }
}

function logger() {
  const entries = [];
  return {
    entries,
    info: (message) => entries.push(['info', message]),
    warn: (message) => entries.push(['warn', message]),
    error: (message) => entries.push(['error', message]),
  };
}

test.beforeEach(() => {
  FakeNotification.supported = true;
  FakeNotification.instances = [];
});

test('Windows 차단 알림은 배너가 사라지지 않게 중요·고정 알림으로 보낸다', () => {
  const log = logger();
  const shown = showSystemNotification({
    Notification: FakeNotification,
    platform: 'win32',
    title: 'Campfire — ChatGPT 전송을 막았습니다',
    body: '검사를 끝내지 못한 그림이라 보내지 않았습니다',
    logger: log,
  });

  assert.equal(shown, true);
  assert.equal(FakeNotification.instances.length, 1);
  assert.deepEqual(FakeNotification.instances[0].options, {
    title: 'Campfire — ChatGPT 전송을 막았습니다',
    body: '검사를 끝내지 못한 그림이라 보내지 않았습니다',
    urgency: 'critical',
    timeoutType: 'never',
  });
  assert.equal(FakeNotification.instances[0].shown, true);
  assert.match(log.entries[0][1], /표시됨/);
});

test('운영체제 알림을 지원하지 않으면 기록하고 대체 알림을 부른다', () => {
  FakeNotification.supported = false;
  const log = logger();
  const fallback = [];

  const shown = showSystemNotification({
    Notification: FakeNotification,
    platform: 'win32',
    title: '제목', body: '본문', logger: log,
    fallback: (reason) => fallback.push(reason),
  });

  assert.equal(shown, false);
  assert.equal(FakeNotification.instances.length, 0);
  assert.deepEqual(fallback, ['not-supported']);
  assert.match(log.entries[0][1], /지원하지 않음/);
});

test('네이티브 알림 생성 실패를 기록하고 대체 알림을 부른다', () => {
  const log = logger();
  const fallback = [];
  showSystemNotification({
    Notification: FakeNotification,
    platform: 'win32',
    title: '제목', body: '본문', logger: log,
    fallback: (reason) => fallback.push(reason),
  });

  FakeNotification.instances[0].emit('failed', {}, 'toast rejected');
  assert.deepEqual(fallback, ['toast rejected']);
  assert.match(log.entries.at(-1)[1], /toast rejected/);
});

test('패키징 앱에서도 알림 표시 결과를 사용자 로그 폴더에 남긴다', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-notification-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const logPath = path.join(root, 'logs', 'desktop-notification.log');
  const log = createFileLogger(logPath);

  log.info('[notification] 표시됨: 테스트');

  assert.match(fs.readFileSync(logPath, 'utf8'), /INFO \[notification\] 표시됨: 테스트/);
});
