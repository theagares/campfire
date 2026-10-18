'use strict';
/**
 * 엔진 로그를 파이프가 아니라 파일 핸들로 넘긴다(engine-manager.js start).
 *
 * 맥 실측: 키체인 승인 창이 떠 있는 동안 앱 메인 프로세스가 멈추면 엔진 stdout 파이프를
 * 아무도 비우지 않아, 버퍼가 찬 뒤 엔진의 로그 쓰기가 막히고 엔진 전체가 응답하지 않았다.
 * 파일 핸들이면 운영체제가 바로 파일에 쓰므로 부모가 멈춰도 자식은 막히지 않는다.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function load(request, parent, isMain) {
  if (request === 'electron') return { app: { isPackaged: false } };
  return originalLoad.call(this, request, parent, isMain);
};
const { openEngineLog, capEngineLog, readLogTail } = require('../main/engine-manager');
Module._load = originalLoad;

const tempLog = () => path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-engine-log-')), 'logs', 'engine.log');

test('부모가 멈춰 있어도 자식은 로그 파일 핸들에 끝까지 쓴다', async () => {
  const p = tempLog();
  const fd = openEngineLog(p);
  const size = 1024 * 1024; // 파이프 버퍼(수십 KB)보다 훨씬 크다
  const child = spawn(process.execPath, ['-e', `process.stdout.write('x'.repeat(${size}))`], {
    stdio: ['ignore', fd, fd],
  });
  fs.closeSync(fd);
  // 이벤트 루프를 막은 채 기다린다 — 키체인 창이 떠 있는 메인 프로세스와 같은 상태.
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline && fs.statSync(p).size < size) { /* busy wait */ }
  assert.equal(fs.statSync(p).size, size, '부모가 멈춘 동안 자식이 다 쓰지 못했다');
  await new Promise((resolve) => child.on('exit', resolve));
});

test('상한을 넘으면 지우지 않고 비우며, 열려 있는 핸들은 계속 이어 쓴다', () => {
  const p = tempLog();
  const fd = openEngineLog(p, 10);
  fs.writeSync(fd, 'x'.repeat(50));
  capEngineLog(p, 10);
  assert.equal(fs.existsSync(p), true, '파일을 지웠다 — 엔진이 사라진 파일에 쓰게 된다');
  assert.equal(fs.statSync(p).size, 0);
  fs.writeSync(fd, 'after');
  fs.closeSync(fd);
  assert.equal(fs.readFileSync(p, 'utf-8'), 'after', '비운 뒤 쓴 내용이 파일 앞에서부터 이어져야 한다');

  fs.writeFileSync(p, 'y'.repeat(50));
  fs.closeSync(openEngineLog(p, 10)); // 시작할 때도 상한을 본다
  assert.equal(fs.statSync(p).size, 0);
});

test('로그 끝부분을 줄 단위로 읽고, 파일이 없으면 빈 목록', () => {
  const p = tempLog();
  assert.deepEqual(readLogTail(p), []);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, `${'old\n'.repeat(5000)}Traceback (most recent call last):\nImportError: boom\n`);
  const lines = readLogTail(p, 64);
  assert.ok(lines.includes('ImportError: boom'), '마지막 줄을 못 읽었다');
  assert.ok(lines.join('\n').length <= 64, '끝부분만 읽어야 한다');
});
