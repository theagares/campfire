// 사이트의 window capture 핸들러가 원본 drop을 먼저 받는 회귀를 막는다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'content', 'content.js'), 'utf8');
const start = source.indexOf('  async function captureDroppedFiles(event) {');
const end = source.indexOf('  // ── 붙여넣기', start);
assert.ok(start >= 0 && end > start, '드롭 캡처 구현을 찾지 못했습니다');
const dropCapture = source.slice(start, end);

function load(hostname) {
  const listeners = { window: [], document: [] };
  const staged = [];
  const cleared = [];
  const sandbox = {
    location: { hostname },
    window: { addEventListener(type, fn, capture) {
      if (type === 'drop') listeners.window.push({ fn, capture });
    } },
    document: { addEventListener(type, fn, capture) {
      if (type === 'drop') listeners.document.push({ fn, capture });
    } },
    stageBatch(sourceType, files) { staged.push({ sourceType, files }); return true; },
    findFileInput() { return null; },
    clearSiteDragState(...args) { cleared.push(args); },
  };
  vm.runInNewContext(dropCapture, sandbox, { filename: 'content-drop.js' });
  return { listeners, staged, cleared };
}

async function deliverDrop(harness, files) {
  const event = {
    dataTransfer: { files }, target: { isConnected: true }, clientX: 10, clientY: 20,
    stopped: false, prevented: false,
    preventDefault() { this.prevented = true; },
    stopImmediatePropagation() { this.stopped = true; },
  };
  let siteReceivedOriginal = false;
  // DOM 전파 순서: window capture → 사이트 window capture → document capture.
  for (const { fn } of harness.listeners.window) {
    await fn(event);
    if (event.stopped) break;
  }
  if (!event.stopped) siteReceivedOriginal = files.length > 0;
  if (!event.stopped) {
    for (const { fn } of harness.listeners.document) {
      await fn(event);
      if (event.stopped) break;
    }
  }
  return { event, siteReceivedOriginal };
}

(async () => {
  const copilot = load('copilot.microsoft.com');
  assert.equal(copilot.listeners.window.length, 1);
  assert.equal(copilot.listeners.window[0].capture, true);
  assert.equal(copilot.listeners.document.length, 0);
  const original = { name: 'private.txt' };
  const result = await deliverDrop(copilot, [original]);
  assert.equal(result.siteReceivedOriginal, false, 'Copilot이 승인 전 원본 drop을 받았습니다');
  assert.equal(result.event.prevented, true);
  assert.equal(copilot.staged.length, 1);
  assert.equal(copilot.staged[0].files[0], original);
  assert.equal(copilot.cleared.length, 1);

  // ChatGPT도 document capture보다 먼저 원본 drop을 소비한다. 실제 브라우저에서
  // Campfire 대기 표시보다 업로드 한도 창이 먼저 뜨며 재현됐다.
  const chatgpt = load('chatgpt.com');
  assert.equal(chatgpt.listeners.window.length, 1);
  assert.equal(chatgpt.listeners.window[0].capture, true);
  assert.equal(chatgpt.listeners.document.length, 0);
  const chatgptResult = await deliverDrop(chatgpt, [original]);
  assert.equal(chatgptResult.siteReceivedOriginal, false, 'ChatGPT가 승인 전 원본 drop을 받았습니다');
  assert.equal(chatgptResult.event.prevented, true);
  assert.equal(chatgpt.staged.length, 1);

  // 파일 없는 합성 drop은 사이트로 흘려보내 오버레이를 정리할 수 있어야 한다.
  const empty = await deliverDrop(copilot, []);
  assert.equal(empty.event.stopped, false);
  assert.equal(copilot.staged.length, 1);

  // 다른 사이트는 기존 document capture 경로를 유지한다.
  const claude = load('claude.ai');
  assert.equal(claude.listeners.window.length, 0);
  assert.equal(claude.listeners.document.length, 1);
  console.log('early drop capture ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
